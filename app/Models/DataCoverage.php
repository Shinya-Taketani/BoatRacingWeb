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
        'has_top3_predictions' => 'boolean',
        'odds_race_count' => 'integer',
        'stage2_prediction_race_count' => 'integer',
    ];

    /**
     * races/results/payouts/predictions/top3_predictions のうち欠けている
     * 項目名の一覧。data_coverageにその日の行自体が無ければnull
     * （未チェック/未来日など）。
     *
     * predictions(1着予測モデル)とtop3_predictions(3着以内予測モデル)は
     * 2026-10-04の2モデル構成化以降、別々に検知する
     * （CLAUDE.md「本番モデルを2本立て構成に変更」参照。片方だけ欠けている
     * 状態を「揃っている」と誤判定しないため）。
     *
     * @return list<string>|null
     */
    public static function missingFieldsFor(string $date): ?array
    {
        $row = self::find($date);

        if ($row === null) {
            return null;
        }

        return array_keys(array_filter([
            'races' => ! $row->has_races,
            'results' => ! $row->has_results,
            'payouts' => ! $row->has_payouts,
            'predictions' => ! $row->has_predictions,
            'top3_predictions' => ! $row->has_top3_predictions,
        ]));
    }

    /**
     * その日について「バッチ失敗が疑われる」とみなせる欠損項目のみを返す。
     * data_coverageの行が無ければnull（未チェック/未来日など。呼び出し側で
     * 「記録なし」として別扱いすること）。
     *
     * strict=true（前日以前）: races/results/payouts/predictions/
     * top3_predictionsの全て。
     * strict=false（当日）: races/predictions/top3_predictionsのみ。
     * results/payoutsはレース終了までに確定していないのが正常なので
     * 対象外にする。
     *
     * @return list<string>|null
     */
    public static function criticalGapsFor(string $date, bool $strict): ?array
    {
        $missing = self::missingFieldsFor($date);

        if ($missing === null) {
            return null;
        }

        return $strict
            ? $missing
            : array_values(array_intersect($missing, ['races', 'predictions', 'top3_predictions']));
    }

    /**
     * [$from, $to]（両端含む）の各日について、races/race_results/payouts/
     * predictions/odds_snapshots の実データを集計し直してdata_coverageを
     * upsertする。has_predictions/has_top3_predictionsは「その日の全レース
     * 数と一致して初めて true」とする（1レースでも欠けていれば false。
     * 中止レースも予測自体は締切前に生成済みのはずなので分母から除外しない）。
     *
     * has_results/has_payoutsは、races.cancelled=true のレースを分母から
     * 除外した「開催されたレース数」と一致して初めて true とする。中止
     * レースは荒天等で実際に走っていないため race_results/payouts が
     * 恒久的に存在せず、全レース数を分母にすると中止日が永久にfalseの
     * ままになってしまうため（CLAUDE.md「中止レースを考慮した
     * has_results/has_payoutsの修正」参照）。
     *
     * has_predictions は $modelVersion（1着予測モデル）、
     * has_top3_predictions は $top3ModelVersion（3着以内予測モデル）を
     * 別々に集計する（2026-10-04の2モデル構成化以降。片方だけ欠けている
     * 状態を検知できるようにするため、1つのフラグに潰さない。
     * CLAUDE.md「本番モデルを2本立て構成に変更」参照）。
     *
     * stage2_prediction_race_countは$stage2ModelVersion（stage2のwinnerモデル）の
     * 存在レース数をそのまま記録する整数カウント（odds_race_countと同じ扱い。
     * booleanフラグにしない理由はクラスdocのstage2_prediction_race_count
     * 列コメント参照）。$stage2ModelVersionがnull/未設定の環境では常に0になる。
     */
    public static function refreshCoverage(
        string $modelVersion,
        string $top3ModelVersion,
        int $stage,
        Carbon $from,
        Carbon $to,
        ?string $stage2ModelVersion = null
    ): void {
        DB::statement(
            <<<'SQL'
            WITH days AS (
                SELECT generate_series(?::date, ?::date, interval '1 day')::date AS race_date
            ),
            race_counts AS (
                SELECT
                    race_date,
                    count(*) AS n_races,
                    count(*) FILTER (WHERE NOT cancelled) AS n_completable_races
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
                -- bet_typeを3連単に絞らない。レースが「不成立」判定になった場合、
                -- 一部の式別だけ払戻が存在し他は存在しない、という混在が起こり得る
                -- （2026-09-19 戸田9R: 3連単/3連複は不成立で払戻無し、2連単のみ
                -- 払戻あり、という実例を確認済み）。has_payoutsは「払戻データの
                -- 投入自体ができたか」の監視が目的であり、式別ごとの取得状況までは
                -- 見ないため、いずれか1式別でも存在すれば取得済みとみなす
                -- （CLAUDE.md「2026-09-19の不成立レースとhas_payoutsの修正」参照）。
                SELECT r.race_date, count(DISTINCT po.race_id) AS n_with_payout
                FROM races r
                JOIN payouts po ON po.race_id = r.id
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
            top3_prediction_counts AS (
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
            ),
            stage2_prediction_counts AS (
                SELECT r.race_date, count(DISTINCT p.race_id) AS n_with_prediction
                FROM races r
                JOIN predictions p
                    ON p.race_id = r.id AND p.model_version = ? AND p.stage = 2
                WHERE r.race_date BETWEEN ? AND ?
                GROUP BY r.race_date
            )
            INSERT INTO data_coverage (
                race_date, has_races, has_results, has_payouts, has_predictions,
                has_top3_predictions, odds_race_count, stage2_prediction_race_count,
                created_at, updated_at
            )
            SELECT
                d.race_date,
                coalesce(rc.n_races, 0) > 0 AS has_races,
                coalesce(rc.n_races, 0) > 0
                    AND coalesce(resc.n_with_results, 0) = coalesce(rc.n_completable_races, 0)
                    AS has_results,
                coalesce(rc.n_races, 0) > 0
                    AND coalesce(pc.n_with_payout, 0) = coalesce(rc.n_completable_races, 0)
                    AS has_payouts,
                coalesce(rc.n_races, 0) > 0 AND coalesce(prc.n_with_prediction, 0) = rc.n_races AS has_predictions,
                coalesce(rc.n_races, 0) > 0 AND coalesce(t3c.n_with_prediction, 0) = rc.n_races AS has_top3_predictions,
                coalesce(oc.n_with_odds, 0) AS odds_race_count,
                coalesce(s2c.n_with_prediction, 0) AS stage2_prediction_race_count,
                now(), now()
            FROM days d
            LEFT JOIN race_counts rc ON rc.race_date = d.race_date
            LEFT JOIN result_counts resc ON resc.race_date = d.race_date
            LEFT JOIN payout_counts pc ON pc.race_date = d.race_date
            LEFT JOIN prediction_counts prc ON prc.race_date = d.race_date
            LEFT JOIN top3_prediction_counts t3c ON t3c.race_date = d.race_date
            LEFT JOIN odds_counts oc ON oc.race_date = d.race_date
            LEFT JOIN stage2_prediction_counts s2c ON s2c.race_date = d.race_date
            ON CONFLICT (race_date) DO UPDATE SET
                has_races = EXCLUDED.has_races,
                has_results = EXCLUDED.has_results,
                has_payouts = EXCLUDED.has_payouts,
                has_predictions = EXCLUDED.has_predictions,
                has_top3_predictions = EXCLUDED.has_top3_predictions,
                odds_race_count = EXCLUDED.odds_race_count,
                stage2_prediction_race_count = EXCLUDED.stage2_prediction_race_count,
                updated_at = now()
            SQL,
            [
                $from->toDateString(), $to->toDateString(),
                $from->toDateString(), $to->toDateString(),
                $from->toDateString(), $to->toDateString(),
                $from->toDateString(), $to->toDateString(),
                $modelVersion, $stage, $from->toDateString(), $to->toDateString(),
                $top3ModelVersion, $stage, $from->toDateString(), $to->toDateString(),
                $from->toDateString(), $to->toDateString(),
                $stage2ModelVersion, $from->toDateString(), $to->toDateString(),
            ]
        );
    }
}
