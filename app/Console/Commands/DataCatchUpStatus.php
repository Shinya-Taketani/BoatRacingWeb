<?php

namespace App\Console\Commands;

use App\Models\DataCoverage;
use App\Support\RaceDate;
use Illuminate\Console\Command;
use Illuminate\Support\Carbon;

/**
 * data:catch-upのバッチが連続して失敗していないかを一目で確認するための
 * 軽量なヘルスチェック。data_coverageを更新はせず、既にある値を読むだけ。
 *
 * 使い方:
 *   php artisan data:catch-up:status        # 表示のみ
 *   php artisan data:catch-up:status -q; echo $?   # 0=OK, 1=NG（cron等から）
 */
class DataCatchUpStatus extends Command
{
    protected $signature = 'data:catch-up:status';

    protected $description = '当日・前日のrace_results等が揃っているか確認する（data:catch-upの失敗検知用）';

    public function handle(): int
    {
        $today = RaceDate::today();
        $yesterday = Carbon::parse($today)->subDay()->toDateString();

        $problems = [];

        foreach ([['前日', $yesterday, true], ['当日', $today, false]] as [$label, $date, $strict]) {
            $gaps = DataCoverage::criticalGapsFor($date, $strict);

            if ($gaps === null) {
                $problems[] = "{$label}({$date}): data_coverageに記録がありません";

                continue;
            }

            if ($gaps !== []) {
                $problems[] = "{$label}({$date}): ".implode(', ', $gaps).' が未取得';
            }
        }

        if ($problems === []) {
            $this->info('OK: 当日・前日ともに異常なし');

            return self::SUCCESS;
        }

        $this->error('NG: '.implode(' / ', $problems));

        return self::FAILURE;
    }
}
