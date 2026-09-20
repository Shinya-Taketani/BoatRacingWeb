<?php

namespace App\Models;

use Illuminate\Database\Eloquent\Model;
use Illuminate\Database\Eloquent\Relations\BelongsTo;
use Illuminate\Database\Eloquent\Relations\HasMany;
use Illuminate\Database\Eloquent\Relations\HasOne;

class Prediction extends Model
{
    protected $casts = [
        'published_at' => 'datetime',
        'cutoff_at' => 'datetime',
    ];

    public function race(): BelongsTo
    {
        return $this->belongsTo(Race::class);
    }

    public function entries(): HasMany
    {
        return $this->hasMany(PredictionEntry::class)->orderBy('lane');
    }

    public function tickets(): HasMany
    {
        return $this->hasMany(PredictionTicket::class)->orderBy('rank');
    }

    public function judgment(): HasOne
    {
        return $this->hasOne(PredictionJudgment::class);
    }
}
