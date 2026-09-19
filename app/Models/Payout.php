<?php

namespace App\Models;

use Illuminate\Database\Eloquent\Model;
use Illuminate\Database\Eloquent\Relations\BelongsTo;

class Payout extends Model
{
    protected $casts = [
        'payout' => 'integer',
        'popularity_rank' => 'integer',
    ];

    public function race(): BelongsTo
    {
        return $this->belongsTo(Race::class);
    }
}
