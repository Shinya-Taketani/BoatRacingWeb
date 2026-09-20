<?php

namespace App\Console\Commands;

use App\Support\RaceDate;
use Illuminate\Console\Command;
use Illuminate\Support\Facades\Process;

/**
 * 当日分の特徴量(v1_basic / v2_recent / v3_relative)を生成する。
 *
 * predictions:generate-today から推論前に呼ばれる想定だが、単体でも
 * 実行できるよう独立したコマンドにしてある。
 */
class FeaturesGenerateToday extends Command
{
    protected $signature = 'features:generate-today {date? : YYYY-MM-DD（省略時は本日）}';

    protected $description = '当日分の特徴量(v1_basic/v2_recent/v3_relative)を生成する';

    /** @var array<string, string> Pythonモジュール => 表示用ラベル */
    private const MODULES = [
        'ml.features.basic' => 'v1_basic',
        'ml.features.recent' => 'v2_recent',
        'ml.features.relative' => 'v3_relative',
    ];

    public function handle(): int
    {
        $date = $this->argument('date') ?? RaceDate::today();

        foreach (self::MODULES as $module => $label) {
            $this->info("Generating {$label} for {$date} via {$module}...");

            $result = Process::path(base_path('ml'))
                ->timeout(300)
                ->run(['uv', 'run', 'python', '-m', $module, $date, '--show', '0']);

            foreach (explode("\n", trim($result->output())) as $line) {
                if ($line !== '') {
                    $this->line($line);
                }
            }

            if ($result->failed()) {
                $this->error("{$label} generation failed: ".trim($result->errorOutput()));

                return self::FAILURE;
            }
        }

        return self::SUCCESS;
    }
}
