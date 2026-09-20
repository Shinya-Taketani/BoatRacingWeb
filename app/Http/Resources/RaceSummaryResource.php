<?php

namespace App\Http\Resources;

use App\Support\ConfidenceGrader;
use Illuminate\Http\Request;
use Illuminate\Http\Resources\Attributes\PreserveKeys;
use Illuminate\Http\Resources\Json\JsonResource;

/**
 * GET /api/races/today の一覧項目。
 *
 * predicted_probabilities は lane(1-6)をキーにした連想配列。全キーが数値の
 * ためLaravelのJsonResourceはデフォルトでarray_values()して詰め直して
 * しまう（filter()/removeMissingValues()）。#[PreserveKeys]でそれを防ぎ、
 * lane番号とp_firstの対応をレスポンス上でも保つ。
 */
#[PreserveKeys]
class RaceSummaryResource extends JsonResource
{
    public function toArray(Request $request): array
    {
        $prediction = $this->prediction;
        $pFirstByLane = $prediction
            ? $prediction->entries->pluck('p_first', 'lane')->all()
            : [];

        $normalizedEntropy = ConfidenceGrader::normalizedEntropy($pFirstByLane);
        $topLane = ConfidenceGrader::topLane($pFirstByLane);

        // 結果はrace_results由来（predictions:judgeの日次バッチを待たず、
        // 締切後すぐに「実際の1着」「予測が当たったか」を出せるようにする）。
        $winnerEntry = $this->raceEntries->first(
            fn ($entry) => $entry->result?->finish_pos === 1
        );
        $resultAvailable = $this->raceEntries->contains(fn ($entry) => $entry->result !== null);

        return [
            'id' => $this->id,
            'stadium_name' => $this->stadium->name,
            'race_no' => $this->race_no,
            'deadline_at' => $this->deadline_at?->toIso8601String(),
            'title' => $this->title,
            'predicted_probabilities' => $pFirstByLane !== [] ? $pFirstByLane : null,
            'top_lane' => $topLane,
            'confidence_grade' => ConfidenceGrader::grade($normalizedEntropy),
            'lane1_risk' => ConfidenceGrader::lane1Risk($pFirstByLane),
            'lane1_risk_level' => ConfidenceGrader::lane1RiskLevel($pFirstByLane),
            'is_upset_pick' => ConfidenceGrader::isUpsetPick($pFirstByLane),
            'result_available' => $resultAvailable,
            'actual_winner_lane' => $winnerEntry?->lane,
            'predicted_hit' => ($topLane !== null && $winnerEntry !== null)
                ? $topLane === $winnerEntry->lane
                : null,
        ];
    }
}
