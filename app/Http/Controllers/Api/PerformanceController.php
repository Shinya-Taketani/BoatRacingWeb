<?php

namespace App\Http\Controllers\Api;

use App\Http\Controllers\Controller;
use Illuminate\Http\JsonResponse;
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
