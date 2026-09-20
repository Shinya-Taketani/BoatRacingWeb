<?php

namespace App\Models;

use Illuminate\Database\Eloquent\Model;
use Illuminate\Database\Eloquent\Relations\BelongsTo;

class RacerPeriod extends Model
{
    protected $casts = [
        'valid_from' => 'datetime',
        'valid_to' => 'datetime',
        'national_win_rate' => 'float',
        'national_win_rate_2' => 'float',
        'local_win_rate' => 'float',
        'local_win_rate_2' => 'float',
    ];

    public function racer(): BelongsTo
    {
        return $this->belongsTo(Racer::class);
    }
}
