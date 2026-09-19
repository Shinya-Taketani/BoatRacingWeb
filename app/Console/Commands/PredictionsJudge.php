<?php

namespace App\Console\Commands;

use Illuminate\Console\Command;
use Illuminate\Support\Facades\Process;

/**
 * 結果(race_results)が確定していて未判定のpredictionsをすべて判定し、
 * prediction_judgments に記録する。対象日の指定はしない
 * （judge.py側が「結果確定済みかつ未判定」を全DBから拾うため）。
 */
class PredictionsJudge extends Command
{
    protected $signature = 'predictions:judge';

    protected $description = '結果が確定した未判定のpredictionsをすべて判定し、prediction_judgmentsに記録する';

    public function handle(): int
    {
        $result = Process::path(base_path('ml'))
            ->timeout(300)
            ->run(['uv', 'run', 'python', '-m', 'ml.models.judge']);

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
