<?php

use Illuminate\Foundation\Inspiring;
use Illuminate\Support\Facades\Artisan;
use Illuminate\Support\Facades\Schedule;

Artisan::command('inspire', function () {
    $this->comment(Inspiring::quote());
})->purpose('Display an inspiring quote');

// 朝のバッチ: 当日のレース一覧を取得し、レースごとのオッズ取得ジョブを予約する。
// ここでScheduleを使うのは「1日1回の朝のキックオフ」のみであり、
// レースごとのオッズ取得タイミング(T-10分/T-5分)はSchedule式では表現できない
// ため CaptureOddsJob 側で dispatch()->delay() により個別予約している。
//
// dailyAt()はデフォルトでapp.timezone(UTC)基準のため、timezone()を明示しないと
// 06:00 JSTのつもりが実際は06:00 UTC(=15:00 JST)に実行されてしまう。
Schedule::command('races:fetch-today')
    ->dailyAt('06:00')
    ->timezone(config('app.race_timezone'));
Schedule::command('odds:schedule-today')
    ->dailyAt('06:05')
    ->timezone(config('app.race_timezone'));

// レース一覧の取り込み(06:00)の後、当日分の特徴量生成→推論を行い、
// predictions/prediction_entries に書き込む。
Schedule::command('predictions:generate-today')
    ->dailyAt('06:10')
    ->timezone(config('app.race_timezone'));

// その日の全レース終了後、結果が確定した予測をまとめて判定する。
Schedule::command('predictions:judge')
    ->dailyAt('23:30')
    ->timezone(config('app.race_timezone'));
