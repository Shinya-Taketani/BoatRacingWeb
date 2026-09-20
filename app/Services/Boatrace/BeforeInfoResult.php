<?php

namespace App\Services\Boatrace;

use Carbon\CarbonImmutable;

final class BeforeInfoResult
{
    /**
     * @param  array<int, array{
     *     lane: int,
     *     weight: float|null,
     *     adjusted_weight: float|null,
     *     exhibit_time: float|null,
     *     tilt: float|null,
     *     propeller_changed: bool,
     *     parts_exchanged: string|null,
     *     course_predicted: int|null,
     *     st_exhibit: float|null,
     * }>  $boats  laneをキーにした配列
     * @param  array{
     *     temperature: float|null,
     *     weather_condition: string|null,
     *     wind_speed: float|null,
     *     wind_direction_code: int|null,
     *     water_temperature: float|null,
     *     wave_height: float|null,
     *     measured_at: CarbonImmutable|null,
     * }  $weather
     */
    public function __construct(
        public readonly CarbonImmutable $capturedAt,
        public readonly array $boats,
        public readonly array $weather,
    ) {}
}
