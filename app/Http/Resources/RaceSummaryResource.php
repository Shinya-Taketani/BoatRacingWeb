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

        return [
            'id' => $this->id,
            'stadium_name' => $this->stadium->name,
            'race_no' => $this->race_no,
            'deadline_at' => $this->deadline_at?->toIso8601String(),
            'title' => $this->title,
            'predicted_probabilities' => $pFirstByLane !== [] ? $pFirstByLane : null,
            'confidence_grade' => ConfidenceGrader::grade($normalizedEntropy),
            'lane1_risk' => ConfidenceGrader::lane1Risk($pFirstByLane),
        ];
    }
}
