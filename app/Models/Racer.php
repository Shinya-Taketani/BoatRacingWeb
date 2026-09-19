<?php

namespace App\Models;

use Illuminate\Database\Eloquent\Model;
use Illuminate\Database\Eloquent\Relations\HasMany;

class Racer extends Model
{
    protected $casts = [
        'birth_date' => 'date',
    ];

    public function raceEntries(): HasMany
    {
        return $this->hasMany(RaceEntry::class);
    }
}
