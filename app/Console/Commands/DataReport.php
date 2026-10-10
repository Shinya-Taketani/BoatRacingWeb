<?php

namespace App\Console\Commands;

use App\Support\RaceDate;
use Illuminate\Console\Command;
use Illuminate\Support\Carbon;
use Illuminate\Support\Facades\DB;

/**
 * 運用フェーズ向けのレポートコマンド。直近N日の運用実態を数字で並べるだけで、
 * OK/NGの判定は一切行わない（判定は data:catch-up:status の役割。stage2・
 * オッズは意図的に部分的カバレッジが正常なため、判定には向かない。
 * CLAUDE.md「本番モデルの改良施策・通算一覧」参照、運用フェーズ移行の経緯）。
 *
 * 2026-10-10、モデル改良を一旦完成として実運用データの蓄積フェーズに
 * 移ったことを受けて新設した。3ヶ月後(2027-01頃)の評価
 * （stage1 vs stage2の本番A/B、confident_top3の実績、stage2実カバレッジ、
 * オッズの再検証）に使う素データを、毎回SQLを書かずに確認できるようにする。
 */
class DataReport extends Command
{
    protected $signature = 'data:report {--days=30 : 直近何日分を対象にするか}';

    protected $description = '直近N日の運用実態（races/results/payouts/predictions/stage2/beforeinfo/odds/'
        .'failed_jobs/judgments）を数字で表示する（判定はしない）';

    public function handle(): int
    {
        $days = max(1, (int) $this->option('days'));
        $to = RaceDate::today();
        $from = Carbon::parse($to)->subDays($days - 1)->toDateString();

        $this->info("data:report: {$from} 〜 {$to}（{$days}日分）");

        $this->reportCoverage($from, $to);
        $this->reportFailedJobs($from, $to);
        $this->reportJudgments($from, $to);

        return self::SUCCESS;
    }

    private function pct(int $n, int $total): string
    {
        if ($total === 0) {
            return 'n/a';
        }

        return sprintf('%.1f%%', $n / $total * 100);
    }

    private function cell(int $n, int $total): string
    {
        return "{$n} (".$this->pct($n, $total).')';
    }

    /**
     * races / results / payouts / predictions / top3_predictions / stage2 /
     * race_before_info(source=live) / odds_snapshots(存在) /
     * odds_snapshots(captured_at <= cutoff_at) を日別に集計する。
     *
     * results/payoutsの比率はn_completable(cancelled=false)を分母にする
     * （DataCoverage::refreshCoverageと同じ理由：中止レースは永久に結果が
     * 来ないため、全レース数を分母にすると意味のない低い比率になる）。
     * それ以外(predictions/top3/stage2/live_beforeinfo/odds)はn_races
     * （全レース数）を分母にする。
     */
    private function reportCoverage(string $from, string $to): void
    {
        $modelVersion = config('ml.prediction_model_version');
        $top3ModelVersion = config('ml.prediction_top3_model_version');
        $stage2ModelVersion = config('ml.prediction_stage2_model_version');

        $rows = DB::select(
            <<<'SQL'
            WITH days AS (
                SELECT generate_series(?::date, ?::date, interval '1 day')::date AS race_date
            ),
            race_counts AS (
                SELECT race_date, count(*) AS n_races,
                       count(*) FILTER (WHERE NOT cancelled) AS n_completable
                FROM races WHERE race_date BETWEEN ? AND ?
                GROUP BY race_date
            ),
            result_counts AS (
                SELECT r.race_date, count(DISTINCT re.race_id) AS n
                FROM races r
                JOIN race_entries re ON re.race_id = r.id
                JOIN race_results rr ON rr.race_entry_id = re.id
                WHERE r.race_date BETWEEN ? AND ?
                GROUP BY r.race_date
            ),
            payout_counts AS (
                SELECT r.race_date, count(DISTINCT po.race_id) AS n
                FROM races r
                JOIN payouts po ON po.race_id = r.id
                WHERE r.race_date BETWEEN ? AND ?
                GROUP BY r.race_date
            ),
            pred_counts AS (
                SELECT r.race_date, count(DISTINCT p.race_id) AS n
                FROM races r
                JOIN predictions p ON p.race_id = r.id AND p.model_version = ? AND p.stage = 1
                WHERE r.race_date BETWEEN ? AND ?
                GROUP BY r.race_date
            ),
            top3_counts AS (
                SELECT r.race_date, count(DISTINCT p.race_id) AS n
                FROM races r
                JOIN predictions p ON p.race_id = r.id AND p.model_version = ? AND p.stage = 1
                WHERE r.race_date BETWEEN ? AND ?
                GROUP BY r.race_date
            ),
            stage2_counts AS (
                SELECT r.race_date, count(DISTINCT p.race_id) AS n
                FROM races r
                JOIN predictions p ON p.race_id = r.id AND p.model_version = ? AND p.stage = 2
                WHERE r.race_date BETWEEN ? AND ?
                GROUP BY r.race_date
            ),
            live_beforeinfo_counts AS (
                SELECT r.race_date, count(DISTINCT rbi.race_id) AS n
                FROM races r
                JOIN race_before_info rbi ON rbi.race_id = r.id AND rbi.source = 'live'
                WHERE r.race_date BETWEEN ? AND ?
                GROUP BY r.race_date
            ),
            odds_any_counts AS (
                SELECT r.race_date, count(DISTINCT os.race_id) AS n
                FROM races r
                JOIN odds_snapshots os ON os.race_id = r.id
                WHERE r.race_date BETWEEN ? AND ?
                GROUP BY r.race_date
            ),
            odds_before_cutoff_counts AS (
                SELECT r.race_date, count(DISTINCT os.race_id) AS n
                FROM races r
                JOIN odds_snapshots os ON os.race_id = r.id
                    AND os.captured_at <= r.deadline_at - interval '10 minutes'
                WHERE r.race_date BETWEEN ? AND ?
                GROUP BY r.race_date
            )
            SELECT
                d.race_date,
                coalesce(rc.n_races, 0) AS n_races,
                coalesce(rc.n_completable, 0) AS n_completable,
                coalesce(resc.n, 0) AS n_results,
                coalesce(pc.n, 0) AS n_payouts,
                coalesce(prc.n, 0) AS n_predictions,
                coalesce(t3c.n, 0) AS n_top3,
                coalesce(s2c.n, 0) AS n_stage2,
                coalesce(lbc.n, 0) AS n_live_beforeinfo,
                coalesce(oac.n, 0) AS n_odds_any,
                coalesce(obc.n, 0) AS n_odds_before_cutoff
            FROM days d
            LEFT JOIN race_counts rc ON rc.race_date = d.race_date
            LEFT JOIN result_counts resc ON resc.race_date = d.race_date
            LEFT JOIN payout_counts pc ON pc.race_date = d.race_date
            LEFT JOIN pred_counts prc ON prc.race_date = d.race_date
            LEFT JOIN top3_counts t3c ON t3c.race_date = d.race_date
            LEFT JOIN stage2_counts s2c ON s2c.race_date = d.race_date
            LEFT JOIN live_beforeinfo_counts lbc ON lbc.race_date = d.race_date
            LEFT JOIN odds_any_counts oac ON oac.race_date = d.race_date
            LEFT JOIN odds_before_cutoff_counts obc ON obc.race_date = d.race_date
            ORDER BY d.race_date
            SQL,
            [
                $from, $to,
                $from, $to,
                $from, $to,
                $from, $to,
                $modelVersion, $from, $to,
                $top3ModelVersion, $from, $to,
                $stage2ModelVersion, $from, $to,
                $from, $to,
                $from, $to,
                $from, $to,
            ]
        );

        $tableRows = [];
        $totals = array_fill_keys(
            ['n_races', 'n_completable', 'n_results', 'n_payouts', 'n_predictions',
                'n_top3', 'n_stage2', 'n_live_beforeinfo', 'n_odds_any', 'n_odds_before_cutoff'],
            0
        );

        foreach ($rows as $row) {
            foreach ($totals as $key => $_) {
                $totals[$key] += (int) $row->{$key};
            }

            $tableRows[] = [
                $row->race_date,
                $row->n_races,
                $this->cell((int) $row->n_results, (int) $row->n_completable),
                $this->cell((int) $row->n_payouts, (int) $row->n_completable),
                $this->cell((int) $row->n_predictions, (int) $row->n_races),
                $this->cell((int) $row->n_top3, (int) $row->n_races),
                $this->cell((int) $row->n_stage2, (int) $row->n_races),
                $this->cell((int) $row->n_live_beforeinfo, (int) $row->n_races),
                $this->cell((int) $row->n_odds_any, (int) $row->n_races),
                $this->cell((int) $row->n_odds_before_cutoff, (int) $row->n_races),
            ];
        }

        $tableRows[] = [
            '合計',
            $totals['n_races'],
            $this->cell($totals['n_results'], $totals['n_completable']),
            $this->cell($totals['n_payouts'], $totals['n_completable']),
            $this->cell($totals['n_predictions'], $totals['n_races']),
            $this->cell($totals['n_top3'], $totals['n_races']),
            $this->cell($totals['n_stage2'], $totals['n_races']),
            $this->cell($totals['n_live_beforeinfo'], $totals['n_races']),
            $this->cell($totals['n_odds_any'], $totals['n_races']),
            $this->cell($totals['n_odds_before_cutoff'], $totals['n_races']),
        ];

        $this->line('');
        $this->line('=== カバレッジ（日別 + 合計） ===');
        $this->line('results/payoutsの比率は中止レースを除いた件数が分母、それ以外はraces総数が分母。');
        $this->line('odds_before_cutoff: captured_at <= deadline_at - 10分 を満たすオッズが存在するレース数'
            .'（T-13分への変更が効いているかの確認用。2026-10-10より前は0件のはず）。');
        $this->table(
            ['日付', 'races', 'results', 'payouts', 'predictions', 'top3_predictions',
                'stage2', 'live_beforeinfo', 'odds_any', 'odds_before_cutoff'],
            $tableRows
        );
    }

    /**
     * failed_jobsをジョブ種別(displayName)ごとに日別集計する。
     * CaptureOddsJobの中止レース起因の失敗は2026-10-10の修正で
     * failed_jobsに残らなくなったため、ここに出るものは本当の失敗
     * （CLAUDE.md「CaptureOddsJobの失敗88件の調査」参照）。
     */
    private function reportFailedJobs(string $from, string $to): void
    {
        $fromDt = Carbon::parse($from, config('app.race_timezone'))->startOfDay()->utc();
        $toDt = Carbon::parse($to, config('app.race_timezone'))->endOfDay()->utc();

        $rows = DB::select(
            <<<'SQL'
            SELECT
                (failed_at AT TIME ZONE 'Asia/Tokyo')::date AS jst_date,
                payload::json ->> 'displayName' AS job_class,
                count(*) AS n
            FROM failed_jobs
            WHERE failed_at BETWEEN ? AND ?
            GROUP BY jst_date, job_class
            ORDER BY jst_date
            SQL,
            [$fromDt, $toDt]
        );

        $jobClasses = collect($rows)->pluck('job_class')->unique()->sort()->values()->all();

        $this->line('');
        $this->line('=== failed_jobs（ジョブ種別ごと、日別 + 合計） ===');

        if ($jobClasses === []) {
            $this->line('(該当なし)');

            return;
        }

        $byDateAndClass = [];
        foreach ($rows as $row) {
            $byDateAndClass[$row->jst_date][$row->job_class] = (int) $row->n;
        }

        $tableRows = [];
        $totals = array_fill_keys($jobClasses, 0);
        foreach ($byDateAndClass as $date => $counts) {
            $line = [$date];
            foreach ($jobClasses as $class) {
                $n = $counts[$class] ?? 0;
                $line[] = $n;
                $totals[$class] += $n;
            }
            $line[] = array_sum($counts);
            $tableRows[] = $line;
        }
        $totalRow = ['合計'];
        foreach ($jobClasses as $class) {
            $totalRow[] = $totals[$class];
        }
        $totalRow[] = array_sum($totals);
        $tableRows[] = $totalRow;

        $this->table(['日付', ...$jobClasses, '合計'], $tableRows);
    }

    /**
     * prediction_judgments(1着予測モデルの的中判定)をstage別・日別に集計する。
     * top3モデルの判定は現状prediction_judgmentsに記録されない
     * （judge.pyはp_firstが入っている行=winnerモデルの行のみを対象にする。
     * CLAUDE.md「本番モデルを2本立て構成に変更」参照）ため、本レポートでは
     * stage1(v3) vs stage2(v5)の1着予測の本番A/Bに使う。
     */
    private function reportJudgments(string $from, string $to): void
    {
        $rows = DB::select(
            <<<'SQL'
            SELECT r.race_date, p.stage, count(*) AS judged, sum(pj.hit::int) AS hits
            FROM prediction_judgments pj
            JOIN predictions p ON p.id = pj.prediction_id
            JOIN races r ON r.id = p.race_id
            WHERE r.race_date BETWEEN ? AND ?
            GROUP BY r.race_date, p.stage
            ORDER BY r.race_date, p.stage
            SQL,
            [$from, $to]
        );

        $this->line('');
        $this->line('=== prediction_judgments（stage別的中率、日別 + 合計） ===');
        $this->line('stage1=v3(当日朝生成)、stage2=v5(締切直前の直前情報を含む再予測、部分カバレッジが正常)。');

        if ($rows === []) {
            $this->line('(該当なし)');

            return;
        }

        $byDate = [];
        foreach ($rows as $row) {
            $byDate[$row->race_date][(int) $row->stage] = [
                'judged' => (int) $row->judged,
                'hits' => (int) $row->hits,
            ];
        }

        $tableRows = [];
        $totals = [1 => ['judged' => 0, 'hits' => 0], 2 => ['judged' => 0, 'hits' => 0]];
        foreach ($byDate as $date => $byStage) {
            $line = [$date];
            foreach ([1, 2] as $stage) {
                $judged = $byStage[$stage]['judged'] ?? 0;
                $hits = $byStage[$stage]['hits'] ?? 0;
                $totals[$stage]['judged'] += $judged;
                $totals[$stage]['hits'] += $hits;
                $line[] = $judged;
                $line[] = $hits;
                $line[] = $judged > 0 ? sprintf('%.2f%%', $hits / $judged * 100) : 'n/a';
            }
            $tableRows[] = $line;
        }

        $totalRow = ['合計'];
        foreach ([1, 2] as $stage) {
            $judged = $totals[$stage]['judged'];
            $hits = $totals[$stage]['hits'];
            $totalRow[] = $judged;
            $totalRow[] = $hits;
            $totalRow[] = $judged > 0 ? sprintf('%.2f%%', $hits / $judged * 100) : 'n/a';
        }
        $tableRows[] = $totalRow;

        $this->table(
            ['日付', 'stage1_judged', 'stage1_hit', 'stage1_rate', 'stage2_judged', 'stage2_hit', 'stage2_rate'],
            $tableRows
        );
    }
}
