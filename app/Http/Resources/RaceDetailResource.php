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
        $prediction = $this->prediction;
        $pFirstByLane = $prediction
            ? $prediction->entries->pluck('p_first', 'lane')->all()
            : [];
        $normalizedEntropy = ConfidenceGrader::normalizedEntropy($pFirstByLane);

        return [
            'id' => $this->id,
            'race_date' => $this->race_date->toDateString(),
            'stadium_name' => $this->stadium->name,
            'race_no' => $this->race_no,
            'deadline_at' => $this->deadline_at?->toIso8601String(),
            'title' => $this->title,
            'event_name' => $this->event_name,
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
                    'p_top3' => $e->p_top3,
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
