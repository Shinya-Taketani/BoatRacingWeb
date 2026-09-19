<?php

namespace App\Models;

use Illuminate\Database\Eloquent\Model;
use Illuminate\Database\Eloquent\Relations\BelongsTo;

class RaceResult extends Model
{
    protected $casts = [
        'st' => 'float',
        'race_time' => 'float',
    ];

    public function raceEntry(): BelongsTo
    {
        return $this->belongsTo(RaceEntry::class);
    }
}
