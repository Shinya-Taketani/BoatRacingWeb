<?php

namespace App\Console\Commands;

use App\Support\RaceDate;
use Illuminate\Console\Command;
use Illuminate\Support\Facades\Process;

/**
 * 当日分の特徴量(v1/v2/v3)を生成した上で、保存済みモデルで推論し、
 * predictions / prediction_entries に書き込む。続けて3連単の買い目
 * (tickets:generate-today)も生成する（predictions -> tickets の依存関係が
 * 明確なため、ここでまとめて実行する）。
 *
 * stage は当面1のみ（締切直前の再予測=stage2は未実装）。
 */
class PredictionsGenerateToday extends Command
{
    protected $signature = 'predictions:generate-today
        {date? : YYYY-MM-DD（省略時は本日）}
        {--model-version= : 省略時は config(ml.prediction_model_version) / .envのPREDICTION_MODEL_VERSION}';

    protected $description = '当日分の特徴量を生成し、モデルで推論してpredictions/prediction_entriesに書き込む';

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

        // 推論の前提となるv1/v2/v3特徴量を当日分だけ先に生成する。
        $featuresExit = $this->call('features:generate-today', ['date' => $date]);
        if ($featuresExit !== self::SUCCESS) {
            $this->error('特徴量生成に失敗したため推論を中止しました。');

            return self::FAILURE;
        }

        $this->info("Predicting {$date} with model_version={$modelVersion}...");

        $result = Process::path(base_path('ml'))
            ->timeout(300)
            ->run(['uv', 'run', 'python', '-m', 'ml.models.predict', $modelVersion, $date]);

        foreach (explode("\n", trim($result->output())) as $line) {
            if ($line !== '') {
                $this->line($line);
            }
        }

        if ($result->failed()) {
            $this->error(trim($result->errorOutput()));

            return self::FAILURE;
        }

        $ticketsExit = $this->call('tickets:generate-today', [
            'date' => $date,
            '--model-version' => $modelVersion,
        ]);
        if ($ticketsExit !== self::SUCCESS) {
            $this->error('買い目生成に失敗しました。');

            return self::FAILURE;
        }

        return self::SUCCESS;
    }
}
