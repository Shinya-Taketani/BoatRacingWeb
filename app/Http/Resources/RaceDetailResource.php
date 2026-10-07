<?php

namespace App\Http\Resources;

use App\Support\ConfidenceGrader;
use Illuminate\Http\Request;
use Illuminate\Http\Resources\Json\JsonResource;

/**
 * GET /api/races/{id} の詳細。出走表 + 予測 + 買い目。
 */
class RaceDetailResource extends JsonResource
{
    public function toArray(Request $request): array
    {
        // 「レースごとにstage2があればそれ、無ければstage1」をここで統一する
        // （2026-10-08、stage2構成。CLAUDE.md「stage2構成」参照）。
        $prediction = $this->effectivePrediction();
        $pFirstByLane = $prediction
            ? $prediction->entries->pluck('p_first', 'lane')->all()
            : [];
        $normalizedEntropy = ConfidenceGrader::normalizedEntropy($pFirstByLane);

        // p_top3は「3着以内モデル」(top3Prediction)から取得する
        // （2026-10-04、2モデル構成に変更。CLAUDE.md「p_top3の直接学習モデル」参照）。
        $top3Prediction = $this->effectiveTop3Prediction();
        $pTop3ByLane = $top3Prediction
            ? $top3Prediction->entries->pluck('p_top3', 'lane')->all()
            : [];

        return [
            'id' => $this->id,
            'race_date' => $this->race_date->toDateString(),
            'stadium_name' => $this->stadium->name,
            'race_no' => $this->race_no,
            'deadline_at' => $this->deadline_at?->toIso8601String(),
            'title' => $this->title,
            'event_name' => $this->event_name,
            'uses_before_info' => $this->usesBeforeInfo(),
            'entries' => $this->raceEntries->map(fn ($entry) => [
                'lane' => $entry->lane,
                'racer_name' => $entry->racer->name,
                'racer_registration_number' => $entry->racer->registration_number,
                'racer_class' => $entry->racerPeriod?->racer_class,
                'age' => $entry->age,
                'motor_no' => $entry->motor_no,
                'motor_win_rate_2' => $entry->motor_win_rate_2,
                'boat_no' => $entry->boat_no,
                'boat_win_rate_2' => $entry->boat_win_rate_2,
                'exhibition_time' => $entry->exhibition_time,
                'weight' => $entry->weight,
                'result' => $entry->result ? [
                    'start_course' => $entry->result->start_course,
                    'st' => $entry->result->st,
                    'finish_pos' => $entry->result->finish_pos,
                    'abnormal_code' => $entry->result->abnormal_code,
                ] : null,
            ])->values(),
            'prediction' => $prediction ? [
                'model_version' => $prediction->model_version,
                'published_at' => $prediction->published_at->toIso8601String(),
                'confidence_grade' => ConfidenceGrader::grade($normalizedEntropy),
                'lane1_risk' => ConfidenceGrader::lane1Risk($pFirstByLane),
                'entries' => $prediction->entries->map(fn ($e) => [
                    'lane' => $e->lane,
                    'p_first' => $e->p_first,
                    'p_top2' => $e->p_top2,
                    'p_top3' => $pTop3ByLane[$e->lane] ?? null,
                ])->values(),
                'tickets' => $prediction->tickets->map(fn ($t) => [
                    'bet_type' => $t->bet_type,
                    'combination' => $t->combination,
                    'est_prob' => $t->est_prob,
                    'rank' => $t->rank,
                ])->values(),
                'judgment' => $prediction->judgment ? [
                    'predicted_lane' => $prediction->judgment->predicted_lane,
                    'actual_lane' => $prediction->judgment->actual_lane,
                    'hit' => $prediction->judgment->hit,
                ] : null,
            ] : null,
        ];
    }
}
