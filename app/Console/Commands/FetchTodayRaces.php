<?php

namespace App\Console\Commands;

use App\Support\RaceDate;
use Illuminate\Console\Command;
use Illuminate\Support\Facades\Process;

/**
 * 当日の番組表(B)を取得し races/race_entries へ投入する。
 *
 * Bファイルの取得・パース・投入はml側(loaders/races.py)に実装済みのため
 * ここでは重複実装せず ml.loaders.cli load-races を呼び出す
 * （K・結果には依存しないので当日朝の時点でも実行できる）。
 */
class FetchTodayRaces extends Command
{
    protected $signature = 'races:fetch-today {date? : YYYY-MM-DD（省略時は本日）}';

    protected $description = '当日のレース一覧(races/race_entries)をmlのload-races経由で取得・投入する';

    public function handle(): int
    {
        $date = $this->argument('date') ?? RaceDate::today();

        $this->info("Fetching races for {$date} via ml load-races...");

        $result = Process::path(base_path('ml'))
            ->timeout(120)
            ->run(['uv', 'run', 'python', '-m', 'ml.loaders.cli', 'load-races', $date]);

        foreach (explode("\n", trim($result->output())) as $line) {
            if ($line !== '') {
                $this->line($line);
            }
        }

        if ($result->failed()) {
            $this->error(trim($result->errorOutput()));

            return self::FAILURE;
        }

        return self::SUCCESS;
    }
}
