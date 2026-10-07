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
 *
 * --stage は2026-10-08、stage2(直前再予測)対応のために追加した。省略時は
 * 従来通り1（日次朝バッチ）。tickets.py自体は以前からp.stage=?で予測を
 * 絞り込んでいたため、Python側の変更は不要だった。
 */
class TicketsGenerateToday extends Command
{
    protected $signature = 'tickets:generate-today
        {date? : YYYY-MM-DD（省略時は本日）}
        {--model-version= : 省略時は config(ml.prediction_model_version) / .envのPREDICTION_MODEL_VERSION}
        {--stage=1 : predictions.stage（省略時は1）}';

    protected $description = '当日分の予測から3連単の買い目を生成し、prediction_ticketsに書き込む';

    public function handle(): int
    {
        $date = $this->argument('date') ?? RaceDate::today();
        $modelVersion = $this->option('model-version') ?: config('ml.prediction_model_version');
        $stage = (int) $this->option('stage');

        if (! $modelVersion) {
            $this->error(
                'model_version が指定されていません。--model-version か、'
                .'.envのPREDICTION_MODEL_VERSION(config(ml.prediction_model_version))を設定してください。'
            );

            return self::FAILURE;
        }

        $this->info("Generating tickets for {$date} with model_version={$modelVersion} stage={$stage}...");

        $result = Process::path(base_path('ml'))
            ->timeout(300)
            ->run([
                config('ml.uv_binary'), 'run', 'python', '-m', 'ml.models.tickets', 'generate',
                $modelVersion, $date, '--stage', (string) $stage,
            ]);

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
