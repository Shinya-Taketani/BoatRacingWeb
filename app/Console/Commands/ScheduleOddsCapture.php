<?php

namespace App\Console\Commands;

use App\Jobs\CaptureOddsJob;
use App\Models\Race;
use App\Support\RaceDate;
use Illuminate\Console\Command;

/**
 * 当日の全レースの deadline_at を読み、各レースごとに
 * 締切のT-10分・T-5分にオッズ取得ジョブ(CaptureOddsJob)を予約投入する。
 *
 * Laravel の Schedule（cron的な定期実行）ではなく、レースごとに個別の
 * dispatch()->delay() で1回限りの実行時刻を予約する。締切時刻はレースごとに
 * バラバラなため、Scheduleの固定cron式では表現できない。
 *
 * 朝1回（レース一覧取得後）に実行する想定:
 *   php artisan races:fetch-today
 *   php artisan odds:schedule-today
 */
class ScheduleOddsCapture extends Command
{
    protected $signature = 'odds:schedule-today {date? : YYYY-MM-DD（省略時は本日）}';

    protected $description = '当日の全レースについて、締切T-10分・T-5分にオッズ取得ジョブを予約する';

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

                continue; // 締切を過ぎたレースは今更予約しても意味がない
            }

            foreach ([10, 5] as $minutesBefore) {
                $captureAt = $race->deadline_at->copy()->subMinutes($minutesBefore);
                CaptureOddsJob::dispatch($race->id)->delay($captureAt);
                $scheduled++;
            }
        }

        $this->info(
            "scheduled {$scheduled} odds-capture job(s) for {$races->count()} race(s) on {$date} ".
            "({$skipped} race(s) already past deadline, skipped)"
        );

        return self::SUCCESS;
    }
}
