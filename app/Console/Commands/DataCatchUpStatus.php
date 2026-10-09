<?php

namespace App\Console\Commands;

use App\Models\DataCoverage;
use App\Support\RaceDate;
use Illuminate\Console\Command;
use Illuminate\Support\Carbon;

/**
 * data:catch-upのバッチが連続して失敗していないかを一目で確認するための
 * 軽量なヘルスチェック。
 *
 * 【2026-10-10修正】以前はdata_coverageを更新せず既存の値を読むだけだった。
 * data_coverageはdata:catch-up（起動時のみ/夜間バッチ）が更新するため、
 * その実行以降に状態が変化していても古いスナップショットのまま判定して
 * しまう。2026-10-09、起動時（05:36 JST）のスナップショットのまま当日の
 * races:fetch-today（06:00 JST）等の結果が反映されておらず「races未取得」
 * という誤報が出た。より危険なのは逆方向（正常時に書かれたスナップショット
 * の後に何かが壊れてもOKを返し続ける）ため、判定前に必ずrefreshCoverage()
 * を呼んで前日・当日の2日分だけ再集計する（refreshCoverage()は集計SQLの
 * 実行のみで外部アクセスは行わないため、頻繁に呼んでも副作用が無いことを
 * 確認済み）。
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

        $modelVersion = config('ml.prediction_model_version');
        $top3ModelVersion = config('ml.prediction_top3_model_version');
        $stage = config('ml.prediction_stage');
        $stage2ModelVersion = config('ml.prediction_stage2_model_version');

        if ($modelVersion && $top3ModelVersion) {
            DataCoverage::refreshCoverage(
                $modelVersion, $top3ModelVersion, $stage,
                Carbon::parse($yesterday), Carbon::parse($today), $stage2ModelVersion
            );
        } else {
            $this->warn('PREDICTION_MODEL_VERSION/PREDICTION_TOP3_MODEL_VERSION未設定のため、再集計をスキップして既存のdata_coverageをそのまま読みます。');
        }

        $problems = [];
        $timestamps = [];

        foreach ([['前日', $yesterday, true], ['当日', $today, false]] as [$label, $date, $strict]) {
            $row = DataCoverage::find($date);
            $timestamps[] = "{$label}({$date}) updated_at=".($row?->updated_at?->toDateTimeString() ?? 'なし');

            $gaps = DataCoverage::criticalGapsFor($date, $strict);

            if ($gaps === null) {
                $problems[] = "{$label}({$date}): data_coverageに記録がありません";

                continue;
            }

            if ($gaps !== []) {
                $problems[] = "{$label}({$date}): ".implode(', ', $gaps).' が未取得';
            }
        }

        $this->line('data_coverage再集計時刻: '.implode(' / ', $timestamps));

        if ($problems === []) {
            $this->info('OK: 当日・前日ともに異常なし');

            return self::SUCCESS;
        }

        $this->error('NG: '.implode(' / ', $problems));

        return self::FAILURE;
    }
}
