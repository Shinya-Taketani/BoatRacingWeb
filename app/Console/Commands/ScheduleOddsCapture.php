<?php

namespace App\Console\Commands;

use App\Jobs\CaptureOddsJob;
use App\Models\Race;
use App\Support\RaceDate;
use Illuminate\Console\Command;

/**
 * 当日の全レースの deadline_at を読み、各レースごとに
 * 締切のT-13分・T-5分にオッズ取得ジョブ(CaptureOddsJob)を予約投入する。
 *
 * Laravel の Schedule（cron的な定期実行）ではなく、レースごとに個別の
 * dispatch()->delay() で1回限りの実行時刻を予約する。締切時刻はレースごとに
 * バラバラなため、Scheduleの固定cron式では表現できない。
 *
 * 【2026-10-10: T-10分→T-13分に変更】cutoff_at(=締切10分前)はリーク防止の
 * 基準点であり、特徴量に使うには captured_at <= cutoff_at を満たす必要がある。
 * 旧T-10分は cutoff_at と同値を狙っていたが、ジョブの実行自体がキュー経由で
 * 数秒〜数十秒遅延するため、実測(1,933レース)で captured_at が cutoff_at
 * より前になったことは0件だった（必ず後になる＝リーク防止を満たせず特徴量に
 * 使えない）。T-13分なら3分の遅延マージンを確保でき、cutoff_atより確実に前の
 * 値として将来の特徴量候補にできる。T-5分（締切直前の最終オッズ、研究用の
 * 参照値）はそのまま維持する。リクエスト数は変わらず1レースあたり2回。
 *
 * 【確認方法】変更を反映した翌日以降のデータで、T-13分側の取得が実際に
 * cutoff_atより前になっているかを確認する:
 *   SELECT count(*) FILTER (WHERE os.captured_at <= r.deadline_at - interval '10 minutes') AS before_cutoff,
 *          count(*) AS total
 *   FROM odds_snapshots os JOIN races r ON r.id = os.race_id
 *   WHERE os.race_id IN (SELECT id FROM races WHERE race_date = '<変更後の日付>')
 *     AND os.captured_at <= r.deadline_at - interval '7 minutes'  -- T-13分狙いの行を大まかに絞る
 *     AND os.captured_at > r.deadline_at - interval '20 minutes';
 *   -- before_cutoff が total と一致すれば全件 cutoff_at より前。
 *   -- 1レース・1艇分のcaptured_atを目視で見るだけでも十分（T-13分側は
 *   -- deadline_at - 13分前後、T-5分側はdeadline_at - 5分前後に分かれるはず）。
 *
 * 朝1回（レース一覧取得後）に実行する想定:
 *   php artisan races:fetch-today
 *   php artisan odds:schedule-today
 */
class ScheduleOddsCapture extends Command
{
    protected $signature = 'odds:schedule-today {date? : YYYY-MM-DD（省略時は本日）}';

    protected $description = '当日の全レースについて、締切T-13分・T-5分にオッズ取得ジョブを予約する';

    public function handle(): int
    {
        $date = $this->argument('date') ?? RaceDate::today();

        $races = Race::whereDate('race_date', $date)->orderBy('deadline_at')->get();

        if ($races->isEmpty()) {
            $this->warn("no races found for {$date}. Run races:fetch-today first.");

            return self::FAILURE;
        }

        $scheduled = 0;
        $skipped = 0;

        foreach ($races as $race) {
            if (now()->greaterThanOrEqualTo($race->deadline_at)) {
                $skipped++;

                continue; // 締切を過ぎたレースは今更予約しても意味がない
            }

            foreach ([13, 5] as $minutesBefore) {
                $captureAt = $race->deadline_at->copy()->subMinutes($minutesBefore);
                CaptureOddsJob::dispatch($race->id)->delay($captureAt);
                $scheduled++;
            }
        }

        $this->info(
            "scheduled {$scheduled} odds-capture job(s) for {$races->count()} race(s) on {$date} ".
            "({$skipped} race(s) already past deadline, skipped)"
        );

        return self::SUCCESS;
    }
}
