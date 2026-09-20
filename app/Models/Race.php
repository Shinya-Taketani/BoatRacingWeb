<?php

namespace App\Models;

use Illuminate\Database\Eloquent\Model;
use Illuminate\Database\Eloquent\Relations\BelongsTo;
use Illuminate\Database\Eloquent\Relations\HasMany;
use Illuminate\Database\Eloquent\Relations\HasOne;

class Race extends Model
{
    protected $casts = [
        'race_date' => 'date',
        'deadline_at' => 'datetime',
    ];

    public function stadium(): BelongsTo
    {
        return $this->belongsTo(Stadium::class);
    }

    public function raceEntries(): HasMany
    {
        return $this->hasMany(RaceEntry::class)->orderBy('lane');
    }

    public function predictions(): HasMany
    {
        return $this->hasMany(Prediction::class);
    }

    /**
     * 本番で使う model_version/stage(config('ml.*')) に絞った予測1件。
     * まだ予測が生成されていないレースではnullになる。
     */
    public function prediction(): HasOne
    {
        return $this->hasOne(Prediction::class)
            ->where('model_version', config('ml.prediction_model_version'))
            ->where('stage', config('ml.prediction_stage'));
    }

    public function payouts(): HasMany
    {
        return $this->hasMany(Payout::class);
    }
}
