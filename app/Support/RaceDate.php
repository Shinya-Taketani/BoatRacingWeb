<?php

namespace App\Support;

/**
 * 「開催日」をJSTの暦日で解決するヘルパ。
 *
 * app.timezone はUTC固定（DBのtimezoneもUTCに揃えている）だが、レース関連の
 * 日付(race_date等)はJSTの暦日であり、UTCの暦日ではない。now()->toDateString()
 * のようなapp.timezone依存の日付取得を「本日の開催日」の判定に使うと、
 * 09:00 JSTより前はUTC側の前日が返ってバグる（CLAUDE.md タイムゾーンのルール）。
 */
class RaceDate
{
    public static function today(): string
    {
        return now(config('app.race_timezone'))->toDateString();
    }
}
