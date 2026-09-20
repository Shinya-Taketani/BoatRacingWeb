<?php

namespace App\Console\Commands;

use App\Support\RaceDate;
use Illuminate\Console\Command;
use Illuminate\Support\Facades\Process;

/**
 * 当日分の予測(predictions/prediction_entries)から、3連単の買い目を
 * 生成しprediction_ticketsに書き込む（ml.models.tickets generate を呼ぶ）。
 *
 * predictions:generate-today の後に実行する前提（予測が無いレースは
 * ml側でスキップされる）。
 */
class TicketsGenerateToday extends Command
{
    protected $signature = 'tickets:generate-today
        {date? : YYYY-MM-DD（省略時は本日）}
        {--model-version= : 省略時は config(ml.prediction_model_version) / .envのPREDICTION_MODEL_VERSION}';

    protected $description = '当日分の予測から3連単の買い目を生成し、prediction_ticketsに書き込む';

    public function handle(): int
    {
        $date = $this->argument('date') ?? RaceDate::today();
        $modelVersion = $this->option('model-version') ?: config('ml.prediction_model_version');

        if (! $modelVersion) {
            $this->error(
                'model_version が指定されていません。--model-version か、'
                .'.envのPREDICTION_MODEL_VERSION(config(ml.prediction_model_version))を設定してください。'
            );

            return self::FAILURE;
        }

        $this->info("Generating tickets for {$date} with model_version={$modelVersion}...");

        $result = Process::path(base_path('ml'))
            ->timeout(300)
            ->run(['uv', 'run', 'python', '-m', 'ml.models.tickets', 'generate', $modelVersion, $date]);

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
