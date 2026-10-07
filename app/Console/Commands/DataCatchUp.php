<?php

namespace App\Console\Commands;

use App\Models\DataCoverage;
use App\Models\Race;
use App\Support\RaceDate;
use Illuminate\Console\Command;
use Illuminate\Support\Carbon;
use Illuminate\Support\Facades\Log;
use Illuminate\Support\Facades\Process;

/**
 * 起動時（電源offで運用しているため、起動のたびにデータが欠けている
 * 可能性がある）に、直近N日分の races/race_results/payouts/predictions/
 * top3_predictions の欠損を検出し、順に埋める。
 *
 * predictions(1着予測モデル)とtop3_predictions(3着以内予測モデル)は
 * 2026-10-04の2モデル構成化以降、別々に検知する（片方だけ欠けている状態を
 * 「揃っている」と誤判定しないため。CLAUDE.md「本番モデルを2本立て構成に
 * 変更」参照）。
 *
 * オッズ(odds_snapshots)は締切後には取得し直せないため埋めようとはせず、
 * data_coverage.odds_race_count に「その日オッズを取得できたレース数」を
 * 記録するだけにとどめる（0なら完全欠損、レース数と一致すれば完全取得）。
 *
 * systemd の boatrace-catchup.service（Type=oneshot, After=postgresql.service）
 * から起動時に一度だけ実行される想定。
 */
class DataCatchUp extends Command
{
    protected $signature = 'data:catch-up {--days=14 : 直近何日分をチェックするか}';

    protected $description = '直近N日の races/race_results/payouts/predictions/top3_predictions の欠損を検出し、順に投入する（オッズは記録のみ）';

    public function handle(): int
    {
        $modelVersion = config('ml.prediction_model_version');
        $top3ModelVersion = config('ml.prediction_top3_model_version');
        $stage = config('ml.prediction_stage');
        // stage2は記録専用(odds_race_countと同じ扱い)なので、未設定でも
        // data:catch-up自体は失敗させない（nullのままrefreshCoverage()に渡すと
        // 常に0件として記録される）。
        $stage2ModelVersion = config('ml.prediction_stage2_model_version');

        if (! $modelVersion) {
            $this->error('PREDICTION_MODEL_VERSION が設定されていません（predictions欠損の補完に必要です）。');

            return self::FAILURE;
        }

        if (! $top3ModelVersion) {
            $this->error('PREDICTION_TOP3_MODEL_VERSION が設定されていません（predictions欠損の補完に必要です）。');

            return self::FAILURE;
        }

        $days = max(1, (int) $this->option('days'));
        $to = RaceDate::today();
        $from = now(config('app.race_timezone'))->subDays($days - 1)->toDateString();

        $this->info("data:catch-up: {$from} 〜 {$to}（{$days}日分）の欠損を検出します。");
        Log::info("data:catch-up start: {$from}..{$to} (days={$days})");

        DataCoverage::refreshCoverage(
            $modelVersion, $top3ModelVersion, $stage, Carbon::parse($from), Carbon::parse($to), $stage2ModelVersion
        );

        $dates = collect();
        for ($cursor = Carbon::parse($from); $cursor->lte(Carbon::parse($to)); $cursor->addDay()) {
            $dates->push($cursor->toDateString());
        }

        foreach ($dates as $date) {
            $this->fillDate($date, $modelVersion, $top3ModelVersion, $stage);
        }

        // race_resultsを取り込んだ直後に判定まで済ませる。23:30の日次バッチを
        // 逃しても翌朝の起動時に埋まるようにする。judge側は「結果確定済みかつ
        // 未判定」のものだけを対象にするため、日次バッチと重複実行しても無害。
        $this->line('未判定の予測を判定します -> predictions:judge を実行');
        $judgeExit = $this->call('predictions:judge');
        Log::info("data:catch-up: predictions:judge exit={$judgeExit}");

        // 全期間まとめて最終状態に更新してからレポートする
        DataCoverage::refreshCoverage(
            $modelVersion, $top3ModelVersion, $stage, Carbon::parse($from), Carbon::parse($to), $stage2ModelVersion
        );
        $this->report($from, $to);
        $this->reportTodayYesterday($to);

        return self::SUCCESS;
    }

    private function fillDate(string $date, string $modelVersion, string $top3ModelVersion, int $stage): void
    {
        $coverage = DataCoverage::find($date);

        if (! $coverage->has_races) {
            $this->line("{$date}: races 欠損 -> races:fetch-today を実行");
            $exit = $this->call('races:fetch-today', ['date' => $date]);
            Log::info("data:catch-up: races:fetch-today {$date} exit={$exit}");

            // 直後にhas_racesだけ更新し、以降の判定に使えるようにする
            DataCoverage::refreshCoverage($modelVersion, $top3ModelVersion, $stage, Carbon::parse($date), Carbon::parse($date));
            $coverage = DataCoverage::find($date);
        }

        if ($coverage->has_races && (! $coverage->has_results || ! $coverage->has_payouts)) {
            $this->line("{$date}: race_results/payouts 欠損 -> ml load-results を実行");

            $result = Process::path(base_path('ml'))
                ->timeout(120)
                ->run([config('ml.uv_binary'), 'run', 'python', '-m', 'ml.loaders.cli', 'load-results', $date]);

            foreach (explode("\n", trim($result->output())) as $line) {
                if ($line !== '') {
                    $this->line("  {$line}");
                }
            }

            if ($result->failed()) {
                // 直近の日付はレースがまだ終わっておらずKファイルが未配信、
                // というのが正常系でも起こりうるため、ここでは処理を止めず
                // ログに残すだけにする（次回起動時に再チェックされる）。
                $this->warn("  {$date}: race_results/payouts の投入に失敗（結果未確定の可能性）: ".trim($result->errorOutput()));
                Log::warning("data:catch-up: load-results {$date} failed: ".trim($result->errorOutput()));
            } else {
                Log::info("data:catch-up: load-results {$date} ok");
            }

            DataCoverage::refreshCoverage($modelVersion, $top3ModelVersion, $stage, Carbon::parse($date), Carbon::parse($date));
            $coverage = DataCoverage::find($date);
        }

        // predictions(1着予測モデル)/top3_predictions(3着以内予測モデル)は
        // どちらか一方でも欠けていれば再実行する。predictions:generate-today
        // は(race_id, model_version, stage)単位で既存分をスキップするため、
        // 片方だけ欠けている状態からの再実行でも無駄なく埋まる
        // （2026-10-04、2モデル構成化。CLAUDE.md「本番モデルを2本立て構成に
        // 変更」参照）。
        if ($coverage->has_races && (! $coverage->has_predictions || ! $coverage->has_top3_predictions)) {
            $missing = collect([
                'predictions' => ! $coverage->has_predictions,
                'top3_predictions' => ! $coverage->has_top3_predictions,
            ])->filter()->keys()->implode(',');
            $this->line("{$date}: {$missing} 欠損 -> predictions:generate-today を実行");
            $exit = $this->call('predictions:generate-today', ['date' => $date]);
            Log::info("data:catch-up: predictions:generate-today {$date} exit={$exit}");
        }
    }

    private function report(string $from, string $to): void
    {
        $rows = DataCoverage::whereBetween('race_date', [$from, $to])->orderBy('race_date')->get();

        $stillMissing = $rows->filter(
            fn (DataCoverage $row) => ! $row->has_races || ! $row->has_results || ! $row->has_payouts
                || ! $row->has_predictions || ! $row->has_top3_predictions
        );

        $this->info('--- data:catch-up 結果 ---');
        if ($stillMissing->isEmpty()) {
            $this->info('races/race_results/payouts/predictions/top3_predictions: 欠損なし');
        } else {
            $this->warn("races/race_results/payouts/predictions/top3_predictions: まだ埋まっていない日付が{$stillMissing->count()}件あります");
            foreach ($stillMissing as $row) {
                $missing = collect([
                    'races' => ! $row->has_races,
                    'results' => ! $row->has_results,
                    'payouts' => ! $row->has_payouts,
                    'predictions' => ! $row->has_predictions,
                    'top3_predictions' => ! $row->has_top3_predictions,
                ])->filter()->keys()->implode(',');
                $this->line("  {$row->race_date->toDateString()}: {$missing}");
            }
        }

        // オッズは補完しない。race数に対する取得数の欠損状況を記録として出すのみ。
        $raceCountByDate = Race::whereBetween('race_date', [$from, $to])
            ->selectRaw('race_date, count(*) as n')
            ->groupBy('race_date')
            ->pluck('n', 'race_date');

        $oddsGaps = $rows->filter(function (DataCoverage $row) use ($raceCountByDate) {
            $total = $raceCountByDate[$row->race_date->toDateString()] ?? 0;

            return $total > 0 && $row->odds_race_count < $total;
        });

        if ($oddsGaps->isEmpty()) {
            $this->info('odds: 欠損なし');
        } else {
            $this->warn("odds: 欠損日が{$oddsGaps->count()}件あります（締切後は再取得不可のため記録のみ）");
            foreach ($oddsGaps as $row) {
                $total = $raceCountByDate[$row->race_date->toDateString()] ?? 0;
                $this->line("  {$row->race_date->toDateString()}: {$row->odds_race_count}/{$total} レース");
            }
        }

        // stage2は補完しない（predictions:generate-stage2の毎分ジョブが
        // 別途担当する）。races数に対する生成数の状況を記録として出すのみ。
        // boolean化していないため、criticalGapsFor()には含まれず
        // data:catch-up:statusの判定には影響しない（CLAUDE.md「stage2構成」参照）。
        $stage2Gaps = $rows->filter(function (DataCoverage $row) use ($raceCountByDate) {
            $total = $raceCountByDate[$row->race_date->toDateString()] ?? 0;

            return $total > 0 && $row->stage2_prediction_race_count < $total;
        });

        if ($stage2Gaps->isEmpty()) {
            $this->info('stage2_predictions: 欠損なし');
        } else {
            $this->warn("stage2_predictions: 未生成日が{$stage2Gaps->count()}件あります（記録のみ、data:catch-upでは補完しません）");
            foreach ($stage2Gaps as $row) {
                $total = $raceCountByDate[$row->race_date->toDateString()] ?? 0;
                $this->line("  {$row->race_date->toDateString()}: {$row->stage2_prediction_race_count}/{$total} レース");
            }
        }

        Log::info('data:catch-up done: '.$stillMissing->count().' date(s) still missing races/results/payouts/predictions/top3_predictions, '
            .$oddsGaps->count().' date(s) with incomplete odds, '
            .$stage2Gaps->count().' date(s) with incomplete stage2_predictions');
    }

    /**
     * 当日・前日だけを目立つ形で末尾に出すサマリ。過去日は「そのうち埋まる」
     * 想定だが、前日分が翌朝になっても揃っていない場合はバッチ失敗の
     * 強いシグナルなので、ログを流し読みしても気づけるようにする。
     */
    private function reportTodayYesterday(string $today): void
    {
        $yesterday = Carbon::parse($today)->subDay()->toDateString();

        $this->line('');
        $this->line(str_repeat('=', 60));
        $this->line('  当日・前日サマリ');
        $this->line(str_repeat('=', 60));

        $this->summarizeDay('前日', $yesterday, strict: true);
        $this->summarizeDay('当日', $today, strict: false);

        $this->line(str_repeat('=', 60));
    }

    private function summarizeDay(string $label, string $date, bool $strict): void
    {
        $missing = DataCoverage::missingFieldsFor($date);

        if ($missing === null) {
            $this->error("  [NG] {$label}({$date}): data_coverageに記録がありません");
            Log::error("data:catch-up: {$label}({$date}) has no data_coverage row");

            return;
        }

        if ($missing === []) {
            $this->info("  [OK] {$label}({$date}): races/results/payouts/predictions/top3_predictions すべて揃っています");

            return;
        }

        // 前日分はもう全部揃っているはずなので、何か欠けていれば厳格に警告する。
        // 当日分は結果・払戻がレース終了までに確定していないのが正常なので、
        // races/predictions/top3_predictions（朝06:00-06:15のバッチで揃う
        // はず）だけを見る。
        $critical = DataCoverage::criticalGapsFor($date, $strict) ?? [];

        if ($critical === []) {
            $this->info(
                "  [--] {$label}({$date}): races/predictions/top3_predictionsは揃っています "
                .'(results/payoutsはレース終了後に確定するため、未取得でも当日中は正常)'
            );

            return;
        }

        $list = implode(', ', $critical);
        $this->error("  [NG] {$label}({$date}): {$list} が未取得です。バッチの失敗が疑われます");
        Log::error("data:catch-up: {$label}({$date}) missing critical fields: {$list}");
    }
}
