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
 * 可能性がある）に、直近N日分の races/race_results/payouts/predictions の
 * 欠損を検出し、順に埋める。
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

    protected $description = '直近N日の races/race_results/payouts/predictions の欠損を検出し、順に投入する（オッズは記録のみ）';

    public function handle(): int
    {
        $modelVersion = config('ml.prediction_model_version');
        $stage = config('ml.prediction_stage');

        if (! $modelVersion) {
            $this->error('PREDICTION_MODEL_VERSION が設定されていません（predictions欠損の補完に必要です）。');

            return self::FAILURE;
        }

        $days = max(1, (int) $this->option('days'));
        $to = RaceDate::today();
        $from = now(config('app.race_timezone'))->subDays($days - 1)->toDateString();

        $this->info("data:catch-up: {$from} 〜 {$to}（{$days}日分）の欠損を検出します。");
        Log::info("data:catch-up start: {$from}..{$to} (days={$days})");

        DataCoverage::refreshCoverage($modelVersion, $stage, Carbon::parse($from), Carbon::parse($to));

        $dates = collect();
        for ($cursor = Carbon::parse($from); $cursor->lte(Carbon::parse($to)); $cursor->addDay()) {
            $dates->push($cursor->toDateString());
        }

        foreach ($dates as $date) {
            $this->fillDate($date, $modelVersion, $stage);
        }

        // 全期間まとめて最終状態に更新してからレポートする
        DataCoverage::refreshCoverage($modelVersion, $stage, Carbon::parse($from), Carbon::parse($to));
        $this->report($from, $to);

        return self::SUCCESS;
    }

    private function fillDate(string $date, string $modelVersion, int $stage): void
    {
        $coverage = DataCoverage::find($date);

        if (! $coverage->has_races) {
            $this->line("{$date}: races 欠損 -> races:fetch-today を実行");
            $exit = $this->call('races:fetch-today', ['date' => $date]);
            Log::info("data:catch-up: races:fetch-today {$date} exit={$exit}");

            // 直後にhas_racesだけ更新し、以降の判定に使えるようにする
            DataCoverage::refreshCoverage($modelVersion, $stage, Carbon::parse($date), Carbon::parse($date));
            $coverage = DataCoverage::find($date);
        }

        if ($coverage->has_races && (! $coverage->has_results || ! $coverage->has_payouts)) {
            $this->line("{$date}: race_results/payouts 欠損 -> ml load-results を実行");

            $result = Process::path(base_path('ml'))
                ->timeout(120)
                ->run(['uv', 'run', 'python', '-m', 'ml.loaders.cli', 'load-results', $date]);

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

            DataCoverage::refreshCoverage($modelVersion, $stage, Carbon::parse($date), Carbon::parse($date));
            $coverage = DataCoverage::find($date);
        }

        if ($coverage->has_races && ! $coverage->has_predictions) {
            $this->line("{$date}: predictions 欠損 -> predictions:generate-today を実行");
            $exit = $this->call('predictions:generate-today', ['date' => $date]);
            Log::info("data:catch-up: predictions:generate-today {$date} exit={$exit}");
        }
    }

    private function report(string $from, string $to): void
    {
        $rows = DataCoverage::whereBetween('race_date', [$from, $to])->orderBy('race_date')->get();

        $stillMissing = $rows->filter(
            fn (DataCoverage $row) => ! $row->has_races || ! $row->has_results || ! $row->has_payouts || ! $row->has_predictions
        );

        $this->info('--- data:catch-up 結果 ---');
        if ($stillMissing->isEmpty()) {
            $this->info('races/race_results/payouts/predictions: 欠損なし');
        } else {
            $this->warn("races/race_results/payouts/predictions: まだ埋まっていない日付が{$stillMissing->count()}件あります");
            foreach ($stillMissing as $row) {
                $missing = collect([
                    'races' => ! $row->has_races,
                    'results' => ! $row->has_results,
                    'payouts' => ! $row->has_payouts,
                    'predictions' => ! $row->has_predictions,
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

        Log::info('data:catch-up done: '.$stillMissing->count().' date(s) still missing races/results/payouts/predictions, '
            .$oddsGaps->count().' date(s) with incomplete odds');
    }
}
