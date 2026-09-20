<?php

namespace App\Console\Commands;

use App\Jobs\CaptureBeforeInfoJob;
use App\Models\Race;
use App\Support\RaceDate;
use Illuminate\Console\Command;

/**
 * 当日の全レースの deadline_at を読み、各レースごとに締切T-12分に
 * 直前情報取得ジョブ(CaptureBeforeInfoJob)を予約投入する。
 *
 * 展示タイム等の公開は締切T-14〜16分頃と実測済み(2026-09-21)なので、
 * T-12であれば通常は既に公開済み。ScheduleOddsCaptureと同じく
 * dispatch()->delay()方式（締切時刻がレースごとにバラバラで
 * Scheduleの固定cron式では表現できないため）。
 */
class ScheduleBeforeInfoCapture extends Command
{
    protected $signature = 'beforeinfo:schedule-today {date? : YYYY-MM-DD（省略時は本日）}';

    protected $description = '当日の全レースについて、締切T-12分に直前情報取得ジョブを予約する';

    public function handle(): int
    {
        $date = $this->argument('date') ?? RaceDate::today();

        $races = Race::whereDate('race_date', $date)->orderBy('deadline_at')->get();

        if ($races->isEmpty()) {
            $this->warn("no races found for {$date}. Run races:fetch-today first.");

            return self::FAILURE;
        }

        $scheduled = 0;
        $skipped = 0;

        foreach ($races as $race) {
            if (now()->greaterThanOrEqualTo($race->deadline_at)) {
                $skipped++;

                continue;
            }

            $captureAt = $race->deadline_at->copy()->subMinutes(12);
            CaptureBeforeInfoJob::dispatch($race->id)->delay($captureAt);
            $scheduled++;
        }

        $this->info(
            "scheduled {$scheduled} beforeinfo-capture job(s) for {$races->count()} race(s) on {$date} ".
            "({$skipped} race(s) already past deadline, skipped)"
        );

        return self::SUCCESS;
    }
}
