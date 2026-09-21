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
 */
class PerformanceController extends Controller
{
    private const BET_TYPE = '3連単';

    public function index(): JsonResponse
    {
        $modelVersion = config('ml.prediction_model_version');
        $stage = config('ml.prediction_stage');

        return response()->json([
            'model_version' => $modelVersion,
            'overall' => $this->summaryRow($modelVersion, $stage),
            'daily' => $this->dailyRows($modelVersion, $stage),
            'monthly' => $this->monthlyRows($modelVersion, $stage),
        ]);
    }

    private function summaryRow(?string $modelVersion, int $stage): array
    {
        $hit = DB::selectOne(
            'SELECT count(*) AS races, count(*) FILTER (WHERE pj.hit) AS hits
             FROM prediction_judgments pj
             JOIN predictions p ON p.id = pj.prediction_id
             WHERE p.model_version = ? AND p.stage = ?',
            [$modelVersion, $stage]
        );

        $recovery = DB::selectOne(
            'SELECT count(DISTINCT t.prediction_id) AS ticket_races,
                    count(t.id) * 100 AS stake, coalesce(sum(po.payout), 0) AS payout
             FROM prediction_tickets t
             JOIN predictions p ON p.id = t.prediction_id
             LEFT JOIN payouts po
                 ON po.race_id = p.race_id AND po.bet_type = t.bet_type AND po.combination = t.combination
             WHERE p.model_version = ? AND p.stage = ? AND t.bet_type = ?',
            [$modelVersion, $stage, self::BET_TYPE]
        );

        return $this->formatRow($hit->races, $hit->hits, $recovery->ticket_races, $recovery->stake, $recovery->payout);
    }

    /**
     * 直近30日。predictions起点だとバッチが丸ごと失敗した日(races はあるが
     * predictionsが1件も無い)が結果から消えてしまいバッチ失敗検知に使えない
     * ため、races起点で日付を確定させ、predictions/judgments/ticketsは
     * LEFT JOINで無ければ0件として出す。
     */
    private function dailyRows(?string $modelVersion, int $stage): array
    {
        $end = RaceDate::today();
        $start = Carbon::parse($end)->subDays(29)->toDateString();

        $rows = DB::select(
            "WITH race_days AS (
                 SELECT race_date FROM races
                 WHERE race_date BETWEEN ? AND ?
                 GROUP BY race_date
             ),
             judgment_agg AS (
                 SELECT r.race_date,
                        count(DISTINCT pj.prediction_id) AS races,
                        count(DISTINCT pj.prediction_id) FILTER (WHERE pj.hit) AS hits
                 FROM predictions p
                 JOIN races r ON r.id = p.race_id
                 JOIN prediction_judgments pj ON pj.prediction_id = p.id
                 WHERE p.model_version = ? AND p.stage = ? AND r.race_date BETWEEN ? AND ?
                 GROUP BY r.race_date
             ),
             ticket_agg AS (
                 SELECT r.race_date,
                        count(DISTINCT t.prediction_id) AS ticket_races,
                        count(t.id) * 100 AS stake,
                        coalesce(sum(po.payout), 0) AS payout
                 FROM predictions p
                 JOIN races r ON r.id = p.race_id
                 JOIN prediction_tickets t ON t.prediction_id = p.id AND t.bet_type = ?
                 LEFT JOIN payouts po
                     ON po.race_id = p.race_id AND po.bet_type = t.bet_type AND po.combination = t.combination
                 WHERE p.model_version = ? AND p.stage = ? AND r.race_date BETWEEN ? AND ?
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
                $modelVersion, $stage, $start, $end,
                self::BET_TYPE, $modelVersion, $stage, $start, $end,
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

    private function monthlyRows(?string $modelVersion, int $stage): array
    {
        $rows = DB::select(
            "SELECT to_char(r.race_date, 'YYYY-MM') AS month,
                    count(DISTINCT pj.prediction_id) AS races,
                    count(DISTINCT pj.prediction_id) FILTER (WHERE pj.hit) AS hits,
                    count(stake_agg.prediction_id) AS ticket_races,
                    coalesce(sum(stake_agg.stake), 0) AS stake,
                    coalesce(sum(stake_agg.payout), 0) AS payout
             FROM predictions p
             JOIN races r ON r.id = p.race_id
             LEFT JOIN prediction_judgments pj ON pj.prediction_id = p.id
             LEFT JOIN (
                 SELECT p2.id AS prediction_id,
                        count(t.id) * 100 AS stake,
                        coalesce(sum(po.payout), 0) AS payout
                 FROM predictions p2
                 JOIN prediction_tickets t ON t.prediction_id = p2.id AND t.bet_type = ?
                 LEFT JOIN payouts po
                     ON po.race_id = p2.race_id AND po.bet_type = t.bet_type AND po.combination = t.combination
                 WHERE p2.model_version = ? AND p2.stage = ?
                 GROUP BY p2.id
             ) stake_agg ON stake_agg.prediction_id = p.id
             WHERE p.model_version = ? AND p.stage = ?
             GROUP BY month
             ORDER BY month",
            [self::BET_TYPE, $modelVersion, $stage, $modelVersion, $stage]
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
        $modelVersion = config('ml.prediction_model_version');
        $stage = config('ml.prediction_stage');
        $options = config('ml.top3_confident_threshold_options');

        $threshold = (float) request()->query('threshold', config('ml.top3_confident_threshold'));
        if (! in_array($threshold, $options, true)) {
            $threshold = config('ml.top3_confident_threshold');
        }

        return response()->json([
            'model_version' => $modelVersion,
            'threshold' => $threshold,
            'threshold_options' => $options,
            'overall' => $this->confidentTop3Overall($modelVersion, $stage, $threshold),
            'daily' => $this->confidentTop3Rows($modelVersion, $stage, $threshold, 'day'),
            'monthly' => $this->confidentTop3Rows($modelVersion, $stage, $threshold, 'month'),
        ]);
    }

    private function confidentTop3Overall(?string $modelVersion, int $stage, float $threshold): array
    {
        $row = DB::selectOne(
            'WITH top_picks AS (
                 SELECT p.race_id, pe.lane, pe.p_top3,
                        rank() OVER (PARTITION BY p.race_id ORDER BY pe.p_top3 DESC) AS rnk
                 FROM prediction_entries pe
                 JOIN predictions p ON p.id = pe.prediction_id
                 WHERE p.model_version = ? AND p.stage = ?
             ),
             confident_picks AS (
                 SELECT race_id, lane FROM top_picks WHERE rnk = 1 AND p_top3 >= ?
             )
             SELECT count(rr.race_entry_id) AS n_confident,
                    count(rr.race_entry_id) FILTER (WHERE rr.finish_pos <= 3) AS n_hit
             FROM confident_picks cp
             JOIN race_entries re ON re.race_id = cp.race_id AND re.lane = cp.lane
             JOIN race_results rr ON rr.race_entry_id = re.id',
            [$modelVersion, $stage, $threshold]
        );

        return $this->formatConfidentRow((int) $row->n_confident, (int) $row->n_hit);
    }

    private function confidentTop3Rows(?string $modelVersion, int $stage, float $threshold, string $granularity): array
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

        $rows = DB::select(
            "WITH race_days AS (
                 SELECT DISTINCT {$dateExpr} AS bucket FROM races {$raceWhere}
             ),
             top_picks AS (
                 SELECT r.race_date, p.race_id, pe.lane, pe.p_top3,
                        rank() OVER (PARTITION BY p.race_id ORDER BY pe.p_top3 DESC) AS rnk
                 FROM prediction_entries pe
                 JOIN predictions p ON p.id = pe.prediction_id
                 JOIN races r ON r.id = p.race_id
                 WHERE p.model_version = ? AND p.stage = ?
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
            [...$raceDaysParams, $modelVersion, $stage, $threshold]
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
