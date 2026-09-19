<?php

namespace App\Models;

use Illuminate\Database\Eloquent\Model;
use Illuminate\Database\Eloquent\Relations\BelongsTo;
use Illuminate\Database\Eloquent\Relations\HasOne;

class RaceEntry extends Model
{
    protected $casts = [
        'motor_win_rate_2' => 'float',
        'boat_win_rate_2' => 'float',
        'exhibition_time' => 'float',
        'weight' => 'float',
    ];

    public function race(): BelongsTo
    {
        return $this->belongsTo(Race::class);
    }

    public function racer(): BelongsTo
    {
        return $this->belongsTo(Racer::class);
    }

    public function racerPeriod(): BelongsTo
    {
        return $this->belongsTo(RacerPeriod::class);
    }

    public function result(): HasOne
    {
        return $this->hasOne(RaceResult::class);
    }
}
