<?php

namespace Tests\Unit\Support;

use App\Support\RaceDate;
use Carbon\Carbon;
use Tests\TestCase;

class RaceDateTest extends TestCase
{
    protected function tearDown(): void
    {
        Carbon::setTestNow();

        parent::tearDown();
    }

    public function test_today_returns_jst_calendar_date_before_utc_date_rolls_over(): void
    {
        // JST 2026-09-18 06:00 = UTC 2026-09-17 21:00（前日）。
        // app.timezone(UTC)基準の now()->toDateString() だと 2026-09-17 になってしまう。
        Carbon::setTestNow(Carbon::create(2026, 9, 17, 21, 0, 0, 'UTC'));

        $this->assertSame('2026-09-18', RaceDate::today());
    }

    public function test_today_matches_utc_date_when_not_near_the_jst_day_boundary(): void
    {
        // JST 2026-09-18 15:00 = UTC 2026-09-18 06:00。この時間帯はJST/UTCどちらの
        // 日付判定でも同じ日になるため、素朴な実装との違いが紛れないよう別途確認する。
        Carbon::setTestNow(Carbon::create(2026, 9, 18, 6, 0, 0, 'UTC'));

        $this->assertSame('2026-09-18', RaceDate::today());
    }
}
