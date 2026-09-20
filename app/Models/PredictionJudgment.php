<?php

namespace App\Models;

use Illuminate\Database\Eloquent\Model;
use Illuminate\Database\Eloquent\Relations\BelongsTo;

class PredictionJudgment extends Model
{
    public $incrementing = false;

    public $timestamps = false;

    protected $primaryKey = 'prediction_id';

    protected $casts = [
        'predicted_lane' => 'integer',
        'actual_lane' => 'integer',
        'hit' => 'boolean',
        'judged_at' => 'datetime',
    ];

    public function prediction(): BelongsTo
    {
        return $this->belongsTo(Prediction::class);
    }
}
