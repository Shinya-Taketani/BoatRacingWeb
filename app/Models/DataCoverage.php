<?php

namespace App\Models;

use Illuminate\Database\Eloquent\Model;
use Illuminate\Support\Carbon;
use Illuminate\Support\Facades\DB;

/**
 * 日付ごとのデータ取得状況（races/race_results/payouts/predictions/odds）。
 * data:catch-up がrefresh()で更新し、欠損検出の元データとして使う。
 */
class DataCoverage extends Model
{
    protected $table = 'data_coverage';

    public $incrementing = false;

    protected $primaryKey = 'race_date';

    protected $keyType = 'string';

    protected $casts = [
        'race_date' => 'date',
        'has_races' => 'boolean',
        'has_results' => 'boolean',
        'has_payouts' => 'boolean',
        'has_predictions' => 'boolean',
        'odds_race_count' => 'integer',
    ];

    /**
     * [$from, $to]（両端含む）の各日について、races/race_results/payouts/
     * predictions/odds_snapshots の実データを集計し直してdata_coverageを
     * upsertする。has_results/has_payouts/has_predictions は「その日の
     * 全レース数と一致して初めて true」とする（1レースでも欠けていれば false）。
     */
    public static function refreshCoverage(string $modelVersion, int $stage, Carbon $from, Carbon $to): void
    {
        DB::statement(
            <<<'SQL'
            WITH days AS (
                SELECT generate_series(?::date, ?::date, interval '1 day')::date AS race_date
            ),
            race_counts AS (
                SELECT race_date, count(*) AS n_races
                FROM races
                WHERE race_date BETWEEN ? AND ?
                GROUP BY race_date
            ),
            result_counts AS (
                SELECT r.race_date, count(DISTINCT re.race_id) AS n_with_results
                FROM races r
                JOIN race_entries re ON re.race_id = r.id
                JOIN race_results rr ON rr.race_entry_id = re.id
                WHERE r.race_date BETWEEN ? AND ?
                GROUP BY r.race_date
            ),
            payout_counts AS (
                SELECT r.race_date, count(DISTINCT po.race_id) AS n_with_payout
                FROM races r
                JOIN payouts po ON po.race_id = r.id AND po.bet_type = '3連単'
                WHERE r.race_date BETWEEN ? AND ?
                GROUP BY r.race_date
            ),
            prediction_counts AS (
                SELECT r.race_date, count(DISTINCT p.race_id) AS n_with_prediction
                FROM races r
                JOIN predictions p
                    ON p.race_id = r.id AND p.model_version = ? AND p.stage = ?
                WHERE r.race_date BETWEEN ? AND ?
                GROUP BY r.race_date
            ),
            odds_counts AS (
                SELECT r.race_date, count(DISTINCT os.race_id) AS n_with_odds
                FROM races r
                JOIN odds_snapshots os ON os.race_id = r.id
                WHERE r.race_date BETWEEN ? AND ?
                GROUP BY r.race_date
            )
            INSERT INTO data_coverage (
                race_date, has_races, has_results, has_payouts, has_predictions,
                odds_race_count, created_at, updated_at
            )
            SELECT
                d.race_date,
                coalesce(rc.n_races, 0) > 0 AS has_races,
                coalesce(rc.n_races, 0) > 0 AND coalesce(resc.n_with_results, 0) = rc.n_races AS has_results,
                coalesce(rc.n_races, 0) > 0 AND coalesce(pc.n_with_payout, 0) = rc.n_races AS has_payouts,
                coalesce(rc.n_races, 0) > 0 AND coalesce(prc.n_with_prediction, 0) = rc.n_races AS has_predictions,
                coalesce(oc.n_with_odds, 0) AS odds_race_count,
                now(), now()
            FROM days d
            LEFT JOIN race_counts rc ON rc.race_date = d.race_date
            LEFT JOIN result_counts resc ON resc.race_date = d.race_date
            LEFT JOIN payout_counts pc ON pc.race_date = d.race_date
            LEFT JOIN prediction_counts prc ON prc.race_date = d.race_date
            LEFT JOIN odds_counts oc ON oc.race_date = d.race_date
            ON CONFLICT (race_date) DO UPDATE SET
                has_races = EXCLUDED.has_races,
                has_results = EXCLUDED.has_results,
                has_payouts = EXCLUDED.has_payouts,
                has_predictions = EXCLUDED.has_predictions,
                odds_race_count = EXCLUDED.odds_race_count,
                updated_at = now()
            SQL,
            [
                $from->toDateString(), $to->toDateString(),
                $from->toDateString(), $to->toDateString(),
                $from->toDateString(), $to->toDateString(),
                $from->toDateString(), $to->toDateString(),
                $modelVersion, $stage, $from->toDateString(), $to->toDateString(),
                $from->toDateString(), $to->toDateString(),
            ]
        );
    }
}
