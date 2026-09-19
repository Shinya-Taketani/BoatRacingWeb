<?php

namespace App\Models;

use Illuminate\Database\Eloquent\Model;
use Illuminate\Database\Eloquent\Relations\BelongsTo;

class PredictionTicket extends Model
{
    public $timestamps = false;

    protected $casts = [
        'est_prob' => 'float',
        'rank' => 'integer',
    ];

    public function prediction(): BelongsTo
    {
        return $this->belongsTo(Prediction::class);
    }
}
