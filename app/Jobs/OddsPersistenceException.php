<?php

namespace App\Jobs;

/**
 * odds_snapshots への書き込み後の検証で captured_at のズレを検知した場合に
 * 送出する。captured_at がズレるとリーク防止（配信時刻カットオフの判定）が
 * 破綻するため、握りつぶさず必ず例外にする。
 */
class OddsPersistenceException extends \RuntimeException {}
