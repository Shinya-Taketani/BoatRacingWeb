<?php

namespace App\Jobs;

use App\Models\Race;
use App\Services\Boatrace\TrifectaOddsResult;
use App\Services\Boatrace\TrifectaOddsScraper;
use Illuminate\Bus\Queueable;
use Illuminate\Contracts\Queue\ShouldQueue;
use Illuminate\Foundation\Bus\Dispatchable;
use Illuminate\Queue\InteractsWithQueue;
use Illuminate\Queue\SerializesModels;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Log;

/**
 * 指定レースの3連単オッズを取得し odds_snapshots へ記録するジョブ。
 *
 * Schedule ではなく dispatch()->delay() でレースごとに個別予約する
 * （ScheduleOddsCapture 参照）。締切を過ぎてから実行しても意味が無いため、
 * リトライは1回までに制限し、実行時に締切を過ぎていれば何もせず終了する。
 */
class CaptureOddsJob implements ShouldQueue
{
    use Dispatchable, InteractsWithQueue, Queueable, SerializesModels;

    /** 失敗しても再試行は1回まで（初回+1回=最大2回実行） */
    public int $tries = 2;

    public int $backoff = 15;

    public function __construct(public readonly int $raceId) {}

    public function handle(TrifectaOddsScraper $scraper): void
    {
        $race = Race::with('stadium')->find($this->raceId);

        if ($race === null) {
            Log::warning("CaptureOddsJob: race_id={$this->raceId} not found; skipping");

            return;
        }

        if (now()->greaterThanOrEqualTo($race->deadline_at)) {
            Log::info(
                "CaptureOddsJob: race_id={$this->raceId} deadline_at={$race->deadline_at} ".
                'already passed; skipping (retrying past the deadline is pointless)'
            );

            return;
        }

        $result = $scraper->fetch($race->stadium->code, $race->race_no, $race->race_date);

        // captured_at は素の Carbon のまま渡してよい。
        // 以前は config/database.php の pgsql 接続に timezone 指定が無く、
        // DBセッションの TimeZone が (config('app.timezone')=UTC と異なる)
        // Asia/Tokyo のままだった。Laravel は DateTimeInterface のバインド値を
        // Grammar::getDateFormat()="Y-m-d H:i:s"（オフセット無しのnaive文字列、
        // UTC基準の桁）で埋め込むため、PostgreSQL側でそれをJSTとして誤解釈し
        // timestamptz カラムが9時間ズレて保存されていた（行数に関係なく発生する
        // バグで、"1行なら平気"に見えたのは検証時に timestamp without time zone
        // 列で確認していた別の現象だった）。'timezone' => 'UTC' を追加し
        // DBセッションを app.timezone と一致させたことで解消済み。
        $rows = array_map(fn (array $o) => [
            'race_id' => $race->id,
            'bet_type' => '3連単',
            'combination' => $o['combination'],
            'odds' => $o['odds'],
            'captured_at' => $result->capturedAt,
            'created_at' => now(),
            'updated_at' => now(),
        ], $result->odds);

        DB::table('odds_snapshots')->upsert(
            $rows,
            ['race_id', 'bet_type', 'combination', 'captured_at'],
            ['odds', 'updated_at'],
        );

        $this->verifyCapturedAt($race, $result);

        Log::info(
            "CaptureOddsJob: race_id={$this->raceId} captured ".count($rows).
            " odds rows at {$result->capturedAt}"
        );
    }

    /**
     * 書き込んだ captured_at を読み戻し、渡した値との差が1秒以上あれば
     * 例外を投げる。captured_at がズレるとリーク防止（配信時刻カットオフの
     * 判定）が破綻するため、静かに間違った値が入ることを許容しない。
     */
    private function verifyCapturedAt(Race $race, TrifectaOddsResult $result): void
    {
        // 同一レースでもT-10分/T-5分等で複数回オッズを取得するため、
        // race_id/bet_type/combination だけでは行を一意に特定できない
        // （captured_at違いで複数行が存在しうる）。今回書き込んだ captured_at
        // でも絞り込み、直前のupsertで実際に書いた行だけを読み戻す。
        $firstCombination = $result->odds[0]['combination'];

        $stored = DB::table('odds_snapshots')
            ->where('race_id', $race->id)
            ->where('bet_type', '3連単')
            ->where('combination', $firstCombination)
            ->where('captured_at', $result->capturedAt)
            ->selectRaw('extract(epoch from captured_at) as epoch')
            ->first();

        if ($stored === null) {
            throw new OddsPersistenceException(
                "CaptureOddsJob: verification failed for race_id={$race->id}: ".
                "row not found after upsert (combination={$firstCombination}, ".
                "captured_at={$result->capturedAt})"
            );
        }

        $expectedEpoch = $result->capturedAt->getTimestampMs() / 1000;
        $storedEpoch = (float) $stored->epoch;
        $diffSeconds = abs($storedEpoch - $expectedEpoch);

        if ($diffSeconds >= 1.0) {
            throw new OddsPersistenceException(
                "CaptureOddsJob: captured_at verification failed for race_id={$race->id}: ".
                "expected epoch={$expectedEpoch}, stored epoch={$storedEpoch}, ".
                "diff={$diffSeconds}s (>= 1s threshold). Refusing to leave a silently ".
                'incorrect captured_at in place (would break leak-prevention cutoff logic).'
            );
        }
    }
}
