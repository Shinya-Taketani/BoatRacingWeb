<?php

namespace App\Console\Commands;

use App\Support\RaceDate;
use Illuminate\Console\Command;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Process;

/**
 * stage2（直前再予測、v5_exhibitionを含む）を生成する。
 *
 * 毎分実行される想定（routes/console.php、withoutOverlapping()付き）。
 * 以前は無条件でuv runを3回(features.exhibition/predict/tickets)起動して
 * いたが、該当レースが無い時間帯でも1回あたり実時間で約0.8秒・CPU時間で
 * 約5.3秒を消費することが判明した（LightGBMモデルのロード等）。1日1440回
 * 呼ばれる以上これは無視できないため、2026-10-08に対象レースの事前チェックを
 * 追加した。
 *
 * 事前チェック(findEligibleRaceIds)で対象レースが0件なら、uv runを一切
 * 呼ばずに即終了する。0件でなければ、その race_id リストだけを
 * ml.features.exhibition --race-ids に渡して特徴量生成の範囲も絞る
 * （以前は「今日1日分」を毎回まるごとupsertしていたため、夕方になるほど
 * 対象行が増え続ける問題があった）。
 *
 * 「対象レース」の定義（事前チェックのSQLと一致させること）:
 * - race_before_info に source='live' の行が、そのレースの race_entries
 *   件数と同数だけ揃っている（ライブ取得が完了している）
 * - races.deadline_at - 10分 > now()（cutoff_atをまだ過ぎていない）
 * - stage2のwinner/top3のどちらかの予測がまだ存在しない
 *   （片方だけ欠けているレースも対象に含める。predict.py側は
 *   モデルごとに独立して既存行をスキップするため、片方だけ埋める
 *   実行でも無駄なく安全）
 */
class PredictionsGenerateStage2 extends Command
{
    protected $signature = 'predictions:generate-stage2
        {date? : YYYY-MM-DD（省略時は本日）}
        {--model-version= : stage2の1着予測モデル。省略時は config(ml.prediction_stage2_model_version)}
        {--top3-model-version= : stage2の3着以内予測モデル。省略時は config(ml.prediction_stage2_top3_model_version)}';

    protected $description = 'ライブ取得済みのv5_exhibition特徴量が揃ったレースから順に、stage2(直前再予測)の特徴量生成->推論->買い目生成を行う';

    public function handle(): int
    {
        $date = $this->argument('date') ?? RaceDate::today();
        $modelVersion = $this->option('model-version') ?: config('ml.prediction_stage2_model_version');
        $top3ModelVersion = $this->option('top3-model-version') ?: config('ml.prediction_stage2_top3_model_version');
        $stage = (int) config('ml.prediction_stage2');

        if (! $modelVersion) {
            $this->error(
                'stage2のmodel_version が指定されていません。--model-version か、'
                .'.envのPREDICTION_STAGE2_MODEL_VERSION(config(ml.prediction_stage2_model_version))を設定してください。'
            );

            return self::FAILURE;
        }

        if (! $top3ModelVersion) {
            $this->error(
                'stage2のtop3_model_version が指定されていません。--top3-model-version か、'
                .'.envのPREDICTION_STAGE2_TOP3_MODEL_VERSION'
                .'(config(ml.prediction_stage2_top3_model_version))を設定してください。'
            );

            return self::FAILURE;
        }

        $raceIds = $this->findEligibleRaceIds($date, $modelVersion, $top3ModelVersion, $stage);

        if ($raceIds === []) {
            $this->info(
                "{$date}: stage2対象レースなし（ライブ取得未完了/cutoff_at超過/生成済みのいずれか）。"
                .'uv runは呼びません。'
            );

            return self::SUCCESS;
        }

        $this->info("{$date}: stage2対象レース ".count($raceIds)."件 (race_id=".implode(',', $raceIds).')');

        // v5_exhibition特徴量を対象レースだけ生成する(--race-ids)。
        $featuresResult = Process::path(base_path('ml'))
            ->timeout(120)
            ->run([
                config('ml.uv_binary'), 'run', 'python', '-m', 'ml.features.exhibition',
                $date, '--show', '0', '--race-ids', ...array_map('strval', $raceIds),
            ]);

        foreach (explode("\n", trim($featuresResult->output())) as $line) {
            if ($line !== '') {
                $this->line($line);
            }
        }

        if ($featuresResult->failed()) {
            $this->error('v5_exhibition特徴量生成に失敗しました: '.trim($featuresResult->errorOutput()));

            return self::FAILURE;
        }

        // 推論自体は「今日1日分、cutoff_at前のみ」のまま呼ぶ。対象はv5特徴量への
        // INNER JOINで既に絞られており、今回生成した対象レース数も少数なので、
        // ここをrace_id絞り込みにする効果は薄い(predict.pyの固定コストは
        // LightGBMモデルのロードが主で、走査行数にはほぼ依存しない)。
        $predictResult = Process::path(base_path('ml'))
            ->timeout(120)
            ->run([
                config('ml.uv_binary'), 'run', 'python', '-m', 'ml.models.predict',
                $modelVersion, $top3ModelVersion, $date,
                '--stage', (string) $stage,
                '--only-before-cutoff',
            ]);

        foreach (explode("\n", trim($predictResult->output())) as $line) {
            if ($line !== '') {
                $this->line($line);
            }
        }

        if ($predictResult->failed()) {
            $this->error('stage2推論に失敗しました: '.trim($predictResult->errorOutput()));

            return self::FAILURE;
        }

        $ticketsExit = $this->call('tickets:generate-today', [
            'date' => $date,
            '--model-version' => $modelVersion,
            '--stage' => (string) $stage,
        ]);
        if ($ticketsExit !== self::SUCCESS) {
            $this->error('stage2買い目生成に失敗しました。');

            return self::FAILURE;
        }

        return self::SUCCESS;
    }

    /**
     * その日のレースのうち、stage2生成の対象になるrace_idを返す。
     *
     * @return list<int>
     */
    private function findEligibleRaceIds(string $date, string $modelVersion, string $top3ModelVersion, int $stage): array
    {
        $rows = DB::select(
            <<<'SQL'
            SELECT r.id
            FROM races r
            WHERE r.race_date = ?
              AND r.deadline_at - interval '10 minutes' > now()
              AND (
                  SELECT count(DISTINCT rbi.lane) FROM race_before_info rbi
                  WHERE rbi.race_id = r.id AND rbi.source = 'live'
              ) = (
                  SELECT count(*) FROM race_entries re WHERE re.race_id = r.id
              )
              AND NOT (
                  EXISTS (
                      SELECT 1 FROM predictions p
                      WHERE p.race_id = r.id AND p.model_version = ? AND p.stage = ?
                  )
                  AND EXISTS (
                      SELECT 1 FROM predictions p
                      WHERE p.race_id = r.id AND p.model_version = ? AND p.stage = ?
                  )
              )
            ORDER BY r.deadline_at
            SQL,
            [$date, $modelVersion, $stage, $top3ModelVersion, $stage]
        );

        return array_map(fn ($row) => (int) $row->id, $rows);
    }
}
