<?php

namespace App\Http\Controllers\Api;

use App\Http\Controllers\Controller;
use App\Http\Resources\RaceDetailResource;
use App\Http\Resources\RaceSummaryResource;
use App\Models\Race;
use App\Support\RaceDate;
use Illuminate\Http\Resources\Json\AnonymousResourceCollection;

class RaceController extends Controller
{
    public function today(): AnonymousResourceCollection
    {
        $races = Race::with([
            'stadium',
            'prediction.entries',
            'top3Prediction.entries',
            'stage2Prediction.entries',
            'stage2Top3Prediction.entries',
            'raceEntries.result',
        ])
            ->whereDate('race_date', RaceDate::today())
            ->orderBy('deadline_at')
            ->get();

        return RaceSummaryResource::collection($races);
    }

    public function show(Race $race): RaceDetailResource
    {
        $race->load([
            'stadium',
            'raceEntries.racer',
            'raceEntries.racerPeriod',
            'raceEntries.result',
            'prediction.entries',
            'prediction.tickets',
            'prediction.judgment',
            'top3Prediction.entries',
            'stage2Prediction.entries',
            'stage2Prediction.tickets',
            'stage2Prediction.judgment',
            'stage2Top3Prediction.entries',
        ]);

        return new RaceDetailResource($race);
    }
}
