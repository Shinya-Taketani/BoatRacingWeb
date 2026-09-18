<?php

namespace App\Services\Boatrace;

use Carbon\CarbonImmutable;

final class TrifectaOddsResult
{
    /**
     * @param  array<int, array{combination: string, odds: float}>  $odds
     */
    public function __construct(
        public readonly CarbonImmutable $capturedAt,
        public readonly array $odds,
    ) {}
}
