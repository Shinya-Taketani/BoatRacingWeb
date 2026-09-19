<?php

namespace App\Models;

use Illuminate\Database\Eloquent\Model;
use Illuminate\Database\Eloquent\Relations\BelongsTo;

/**
 * 複合主キー(prediction_id, lane)・timestampsなし。読み取り専用でしか
 * 使わないため、Eloquentの単一主キー前提はprediction_id側に寄せておく
 * （find()等の単体主キー操作はこのモデルでは行わない）。
 */
class PredictionEntry extends Model
{
    public $incrementing = false;

    public $timestamps = false;

    protected $primaryKey = 'prediction_id';

    protected $casts = [
        'lane' => 'integer',
        'p_first' => 'float',
        'p_top2' => 'float',
        'p_top3' => 'float',
    ];

    public function prediction(): BelongsTo
    {
        return $this->belongsTo(Prediction::class);
    }
}
