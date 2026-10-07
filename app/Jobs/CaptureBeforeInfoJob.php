<?php

namespace App\Jobs;

use App\Models\Race;
use App\Services\Boatrace\BeforeInfoFetchException;
use App\Services\Boatrace\BeforeInfoResult;
use App\Services\Boatrace\BeforeInfoScraper;
use Illuminate\Bus\Queueable;
use Illuminate\Contracts\Queue\ShouldQueue;
use Illuminate\Foundation\Bus\Dispatchable;
use Illuminate\Queue\InteractsWithQueue;
use Illuminate\Queue\SerializesModels;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Log;

/**
 * 指定レースの直前情報(展示タイム・ST展示・気象)を取得し
 * race_before_info / race_weather_info へ記録するジョブ。
 *
 * CaptureOddsJobと同じくdispatch()->delay()でレースごとに個別予約する
 * （ScheduleBeforeInfoCapture参照）。締切を過ぎてから実行しても
 * 意味が無いため、リトライは1回までに制限し、実行時に締切を過ぎていれば
 * 何もせず終了する。
 *
 * 当面は記録のみで、特徴量やpredictionsには使わない（2026-09-21時点）。
 */
class CaptureBeforeInfoJob implements ShouldQueue
{
    use Dispatchable, InteractsWithQueue, Queueable, SerializesModels;

    public int $tries = 2;

    public int $backoff = 15;

    public function __construct(public readonly int $raceId) {}

    public function handle(BeforeInfoScraper $scraper): void
    {
        $race = Race::with('stadium')->find($this->raceId);

        if ($race === null) {
            Log::warning("CaptureBeforeInfoJob: race_id={$this->raceId} not found; skipping");

            return;
        }

        if (now()->greaterThanOrEqualTo($race->deadline_at)) {
            Log::info(
                "CaptureBeforeInfoJob: race_id={$this->raceId} deadline_at={$race->deadline_at} ".
                'already passed; skipping'
            );

            return;
        }

        try {
            $result = $scraper->fetch($race->stadium->code, $race->race_no, $race->race_date);
        } catch (BeforeInfoFetchException $e) {
            Log::warning("CaptureBeforeInfoJob: race_id={$this->raceId} fetch failed: {$e->getMessage()}");

            throw $e; // tries=2でリトライさせる
        }

        $this->storeBoats($race, $result);
        $this->storeWeather($race, $result);

        Log::info(
            "CaptureBeforeInfoJob: race_id={$this->raceId} captured ".count($result->boats).
            " boat row(s) at {$result->capturedAt}"
        );
    }

    private function storeBoats(Race $race, BeforeInfoResult $result): void
    {
        $rows = array_map(fn (array $b) => [
            'race_id' => $race->id,
            'lane' => $b['lane'],
            'weight' => $b['weight'],
            'adjusted_weight' => $b['adjusted_weight'],
            'exhibit_time' => $b['exhibit_time'],
            'tilt' => $b['tilt'],
            'propeller_changed' => $b['propeller_changed'],
            'parts_exchanged' => $b['parts_exchanged'],
            'course_predicted' => $b['course_predicted'],
            'st_exhibit' => $b['st_exhibit'],
            'captured_at' => $result->capturedAt,
            // このジョブは締切T-12分の予約実行なので、captured_atは常に
            // 実際の公開時刻に近い「ライブ取得」。バックフィル(過去分の後日取得)
            // とは区別する(ml.features.exhibitionのリーク検証がsourceで分岐する)。
            'source' => 'live',
            'created_at' => now(),
            'updated_at' => now(),
        ], array_values($result->boats));

        DB::table('race_before_info')->upsert(
            $rows,
            ['race_id', 'lane'],
            [
                'weight', 'adjusted_weight', 'exhibit_time', 'tilt', 'propeller_changed',
                'parts_exchanged', 'course_predicted', 'st_exhibit', 'captured_at', 'source',
                'updated_at',
            ],
        );
    }

    private function storeWeather(Race $race, BeforeInfoResult $result): void
    {
        $w = $result->weather;

        DB::table('race_weather_info')->upsert([[
            'race_id' => $race->id,
            'temperature' => $w['temperature'],
            'weather_condition' => $w['weather_condition'],
            'wind_speed' => $w['wind_speed'],
            'wind_direction_code' => $w['wind_direction_code'],
            'water_temperature' => $w['water_temperature'],
            'wave_height' => $w['wave_height'],
            'measured_at' => $w['measured_at'],
            'captured_at' => $result->capturedAt,
            'created_at' => now(),
            'updated_at' => now(),
        ]], ['race_id'], [
            'temperature', 'weather_condition', 'wind_speed', 'wind_direction_code',
            'water_temperature', 'wave_height', 'measured_at', 'captured_at', 'updated_at',
        ]);
    }
}
