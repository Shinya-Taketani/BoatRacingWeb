<?php

namespace App\Http\Controllers\Api;

use App\Http\Controllers\Controller;
use App\Support\RaceDate;
use Illuminate\Http\JsonResponse;
use Illuminate\Support\Carbon;
use Illuminate\Support\Facades\DB;

/**
 * 実績サマリ。的中率は prediction_judgments（1着予測が当たったか）、
 * 回収率は prediction_tickets(3連単) と payouts の突合で算出する
 * （ml/src/ml/models/tickets.py の overall_recovery() と同じ定義）。
 * CLAUDE.md「プロダクト方針: 的中率と回収率は必ず併記する」に従い、
 * 常に両方を返す。
 *
 * 2026-10-08、stage2(v5_exhibitionを含む直前再予測)対応により、
 * 「レースごとにstage2の予測があればそれ、無ければstage1」を全SQLで
 * 統一した（CLAUDE.md「stage2構成」参照）。実装は「stage1/stage2の
 * model_versionのどちらかに一致し、stage(1,2)が揃うpredictions行から
 * DISTINCT ON (race_id) ... ORDER BY race_id, stage DESC でレースごとに
 * 1行だけ選ぶ」CTE(active_winner/active_top3)を経由する方式。
 *
 * 重要: judge(predictions:judge)はstage1/stage2の両方を独立に判定するため、
 * stage2が存在するレースはprediction_judgmentsに2行（stage1分・stage2分）
 * 入っている。上記のactive_winner CTEで必ずレースごとに1つのprediction_id
 * へ絞ってからjudgments/ticketsをJOINすることで、二重計上を防いでいる
 * （model_version IN (...) で素朴にWHERE句だけ絞ると、同一レースの
 * stage1行とstage2行が両方ヒットして二重計上になるため、必ずこのCTEを
 * 経由すること）。
 *
 * 過去日(stage2が一度も生成されていない日)は active_winner/active_top3が
 * 常にstage1の行だけを選ぶため、集計値は2026-10-08以前の実装と完全に
 * 一致する（回帰確認済み）。
 */
class PerformanceController extends Controller
{
    private const BET_TYPE = '3連単';

    public function index(): JsonResponse
    {
        $stage1Version = config('ml.prediction_model_version');
        $stage2Version = config('ml.prediction_stage2_model_version');

        return response()->json([
            'model_version' => $stage1Version,
            'overall' => $this->summaryRow($stage1Version, $stage2Version),
            'daily' => $this->dailyRows($stage1Version, $stage2Version),
            'monthly' => $this->monthlyRows($stage1Version, $stage2Version),
        ]);
    }

    /**
     * レースごとに「stage2があればそれ、無ければstage1」の予測1件だけを選ぶCTE。
     * $modelVersion1/$modelVersion2 は同じ種類(winner同士、またはtop3同士)の
     * stage1/stage2 model_versionを渡すこと。$modelVersion2がnull(stage2未設定)
     * でも安全（model_version = NULL は何にも一致しないため、stage1のみの
     * 挙動にそのままフォールバックする）。
     */
    private function activePredictionCte(): string
    {
        return 'SELECT DISTINCT ON (race_id) id, race_id
                FROM predictions
                WHERE model_version IN (?, ?) AND stage IN (1, 2)
                ORDER BY race_id, stage DESC';
    }

    private function summaryRow(?string $stage1Version, ?string $stage2Version): array
    {
        $activeWinner = $this->activePredictionCte();

        $hit = DB::selectOne(
            "WITH active_winner AS ({$activeWinner})
             SELECT count(*) AS races, count(*) FILTER (WHERE pj.hit) AS hits
             FROM active_winner a
             JOIN prediction_judgments pj ON pj.prediction_id = a.id",
            [$stage1Version, $stage2Version]
        );

        $recovery = DB::selectOne(
            "WITH active_winner AS ({$activeWinner})
             SELECT count(DISTINCT t.prediction_id) AS ticket_races,
                    count(t.id) * 100 AS stake, coalesce(sum(po.payout), 0) AS payout
             FROM active_winner a
             JOIN prediction_tickets t ON t.prediction_id = a.id AND t.bet_type = ?
             LEFT JOIN payouts po
                 ON po.race_id = a.race_id AND po.bet_type = t.bet_type AND po.combination = t.combination",
            [$stage1Version, $stage2Version, self::BET_TYPE]
        );

        return $this->formatRow($hit->races, $hit->hits, $recovery->ticket_races, $recovery->stake, $recovery->payout);
    }

    /**
     * 直近30日。predictions起点だとバッチが丸ごと失敗した日(races はあるが
     * predictionsが1件も無い)が結果から消えてしまいバッチ失敗検知に使えない
     * ため、races起点で日付を確定させ、predictions/judgments/ticketsは
     * LEFT JOINで無ければ0件として出す。
     */
    private function dailyRows(?string $stage1Version, ?string $stage2Version): array
    {
        $end = RaceDate::today();
        $start = Carbon::parse($end)->subDays(29)->toDateString();
        $activeWinner = $this->activePredictionCte();

        $rows = DB::select(
            "WITH race_days AS (
                 SELECT race_date FROM races
                 WHERE race_date BETWEEN ? AND ?
                 GROUP BY race_date
             ),
             active_winner AS ({$activeWinner}),
             judgment_agg AS (
                 SELECT r.race_date,
                        count(DISTINCT pj.prediction_id) AS races,
                        count(DISTINCT pj.prediction_id) FILTER (WHERE pj.hit) AS hits
                 FROM active_winner a
                 JOIN races r ON r.id = a.race_id
                 JOIN prediction_judgments pj ON pj.prediction_id = a.id
                 WHERE r.race_date BETWEEN ? AND ?
                 GROUP BY r.race_date
             ),
             ticket_agg AS (
                 SELECT r.race_date,
                        count(DISTINCT t.prediction_id) AS ticket_races,
                        count(t.id) * 100 AS stake,
                        coalesce(sum(po.payout), 0) AS payout
                 FROM active_winner a
                 JOIN races r ON r.id = a.race_id
                 JOIN prediction_tickets t ON t.prediction_id = a.id AND t.bet_type = ?
                 LEFT JOIN payouts po
                     ON po.race_id = a.race_id AND po.bet_type = t.bet_type AND po.combination = t.combination
                 WHERE r.race_date BETWEEN ? AND ?
                 GROUP BY r.race_date
             )
             SELECT to_char(rd.race_date, 'YYYY-MM-DD') AS date,
                    coalesce(ja.races, 0) AS races,
                    coalesce(ja.hits, 0) AS hits,
                    coalesce(ta.ticket_races, 0) AS ticket_races,
                    coalesce(ta.stake, 0) AS stake,
                    coalesce(ta.payout, 0) AS payout
             FROM race_days rd
             LEFT JOIN judgment_agg ja ON ja.race_date = rd.race_date
             LEFT JOIN ticket_agg ta ON ta.race_date = rd.race_date
             ORDER BY rd.race_date DESC",
            [
                $start, $end,
                $stage1Version, $stage2Version,
                $start, $end,
                self::BET_TYPE,
                $start, $end,
            ]
        );

        return array_map(
            fn ($row) => [
                'date' => $row->date,
                ...$this->formatRow($row->races, $row->hits, $row->ticket_races, $row->stake, $row->payout),
            ],
            $rows
        );
    }

    private function monthlyRows(?string $stage1Version, ?string $stage2Version): array
    {
        $activeWinner = $this->activePredictionCte();

        $rows = DB::select(
            "WITH active_winner AS ({$activeWinner})
             SELECT to_char(r.race_date, 'YYYY-MM') AS month,
                    count(DISTINCT pj.prediction_id) AS races,
                    count(DISTINCT pj.prediction_id) FILTER (WHERE pj.hit) AS hits,
                    count(stake_agg.prediction_id) AS ticket_races,
                    coalesce(sum(stake_agg.stake), 0) AS stake,
                    coalesce(sum(stake_agg.payout), 0) AS payout
             FROM active_winner a
             JOIN races r ON r.id = a.race_id
             LEFT JOIN prediction_judgments pj ON pj.prediction_id = a.id
             LEFT JOIN (
                 SELECT a2.id AS prediction_id,
                        count(t.id) * 100 AS stake,
                        coalesce(sum(po.payout), 0) AS payout
                 FROM active_winner a2
                 JOIN prediction_tickets t ON t.prediction_id = a2.id AND t.bet_type = ?
                 LEFT JOIN payouts po
                     ON po.race_id = a2.race_id AND po.bet_type = t.bet_type AND po.combination = t.combination
                 GROUP BY a2.id
             ) stake_agg ON stake_agg.prediction_id = a.id
             GROUP BY month
             ORDER BY month",
            [$stage1Version, $stage2Version, self::BET_TYPE]
        );

        return array_map(
            fn ($row) => [
                'month' => $row->month,
                ...$this->formatRow($row->races, $row->hits, $row->ticket_races, $row->stake, $row->payout),
            ],
            $rows
        );
    }

    /**
     * 「3着内確実な艇」(config('ml.top3_confident_threshold')、軸艇)の的中率を
     * 日別・月別に集計する。races起点(dailyRows()と同じ理由)で、predictionsが
     * 無い日・該当艇が無いレースも0件として自然に出す。
     */
    public function confidentTop3(): JsonResponse
    {
        // p_top3は「3着以内モデル」(prediction_top3_model_version)から取得する。
        // 1着予測モデル(prediction_model_version)とは別のpredictionsレコード
        // （2026-10-04、2モデル構成に変更。CLAUDE.md「p_top3の直接学習モデル」参照）。
        $stage1Version = config('ml.prediction_top3_model_version');
        $stage2Version = config('ml.prediction_stage2_top3_model_version');
        $options = config('ml.top3_confident_threshold_options');

        $threshold = (float) request()->query('threshold', config('ml.top3_confident_threshold'));
        if (! in_array($threshold, $options, true)) {
            $threshold = config('ml.top3_confident_threshold');
        }

        return response()->json([
            'model_version' => $stage1Version,
            'threshold' => $threshold,
            'threshold_options' => $options,
            'overall' => $this->confidentTop3Overall($stage1Version, $stage2Version, $threshold),
            'daily' => $this->confidentTop3Rows($stage1Version, $stage2Version, $threshold, 'day'),
            'monthly' => $this->confidentTop3Rows($stage1Version, $stage2Version, $threshold, 'month'),
        ]);
    }

    private function confidentTop3Overall(?string $stage1Version, ?string $stage2Version, float $threshold): array
    {
        $activeTop3 = $this->activePredictionCte();

        $row = DB::selectOne(
            "WITH active_top3 AS ({$activeTop3}),
             top_picks AS (
                 SELECT a.race_id, pe.lane, pe.p_top3,
                        rank() OVER (PARTITION BY a.race_id ORDER BY pe.p_top3 DESC) AS rnk
                 FROM prediction_entries pe
                 JOIN active_top3 a ON a.id = pe.prediction_id
             ),
             confident_picks AS (
                 SELECT race_id, lane FROM top_picks WHERE rnk = 1 AND p_top3 >= ?
             )
             SELECT count(rr.race_entry_id) AS n_confident,
                    count(rr.race_entry_id) FILTER (WHERE rr.finish_pos <= 3) AS n_hit
             FROM confident_picks cp
             JOIN race_entries re ON re.race_id = cp.race_id AND re.lane = cp.lane
             JOIN race_results rr ON rr.race_entry_id = re.id",
            [$stage1Version, $stage2Version, $threshold]
        );

        return $this->formatConfidentRow((int) $row->n_confident, (int) $row->n_hit);
    }

    private function confidentTop3Rows(?string $stage1Version, ?string $stage2Version, float $threshold, string $granularity): array
    {
        $dateExpr = $granularity === 'month' ? "to_char(race_date, 'YYYY-MM')" : "to_char(race_date, 'YYYY-MM-DD')";
        $keyName = $granularity === 'month' ? 'month' : 'date';

        // dailyRows()と同じ方針: 日別は直近30日、月別は全期間。
        $raceWhere = '';
        $raceDaysParams = [];
        if ($granularity === 'day') {
            $end = RaceDate::today();
            $start = Carbon::parse($end)->subDays(29)->toDateString();
            $raceWhere = 'WHERE race_date BETWEEN ? AND ?';
            $raceDaysParams = [$start, $end];
        }

        $activeTop3 = $this->activePredictionCte();

        $rows = DB::select(
            "WITH race_days AS (
                 SELECT DISTINCT {$dateExpr} AS bucket FROM races {$raceWhere}
             ),
             active_top3 AS ({$activeTop3}),
             top_picks AS (
                 SELECT r.race_date, a.race_id, pe.lane, pe.p_top3,
                        rank() OVER (PARTITION BY a.race_id ORDER BY pe.p_top3 DESC) AS rnk
                 FROM prediction_entries pe
                 JOIN active_top3 a ON a.id = pe.prediction_id
                 JOIN races r ON r.id = a.race_id
             ),
             confident_picks AS (
                 SELECT race_date, race_id, lane FROM top_picks WHERE rnk = 1 AND p_top3 >= ?
             ),
             judged AS (
                 SELECT {$dateExpr} AS bucket,
                        (rr.finish_pos <= 3) AS hit
                 FROM confident_picks cp
                 JOIN race_entries re ON re.race_id = cp.race_id AND re.lane = cp.lane
                 JOIN race_results rr ON rr.race_entry_id = re.id
             )
             SELECT rd.bucket,
                    count(j.hit) AS n_confident,
                    count(j.hit) FILTER (WHERE j.hit) AS n_hit
             FROM race_days rd
             LEFT JOIN judged j ON j.bucket = rd.bucket
             GROUP BY rd.bucket
             ORDER BY rd.bucket DESC",
            [...$raceDaysParams, $stage1Version, $stage2Version, $threshold]
        );

        return array_map(
            fn ($row) => [
                $keyName => $row->bucket,
                ...$this->formatConfidentRow((int) $row->n_confident, (int) $row->n_hit),
            ],
            $rows
        );
    }

    private function formatConfidentRow(int $nConfident, int $nHit): array
    {
        return [
            'confident_races' => $nConfident,
            'hit' => $nHit,
            'hit_rate' => $nConfident > 0 ? round($nHit / $nConfident, 4) : null,
        ];
    }

    private function formatRow(int $races, int $hits, int $ticketRaces, int $stake, int $payout): array
    {
        $tickets = $stake > 0 ? intdiv($stake, 100) : 0;

        return [
            'races' => $races,
            'hit_rate' => $races > 0 ? round($hits / $races, 4) : null,
            'tickets' => $tickets,
            'stake' => $stake,
            'payout' => $payout,
            'recovery_rate' => $stake > 0 ? round($payout / $stake, 4) : null,
            // 「1レースあたり平均」（3,416万円より、1レース850円買って平均640円戻る
            // という粒度の方が実感が湧く、という理由で追加）
            'avg_tickets_per_race' => $ticketRaces > 0 ? round($tickets / $ticketRaces, 2) : null,
            'avg_stake_per_race' => $ticketRaces > 0 ? round($stake / $ticketRaces, 1) : null,
            'avg_payout_per_race' => $ticketRaces > 0 ? round($payout / $ticketRaces, 1) : null,
        ];
    }
}
