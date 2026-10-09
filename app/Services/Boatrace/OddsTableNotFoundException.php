<?php

namespace App\Services\Boatrace;

/**
 * 3連単オッズテーブルがページ内に存在しなかった場合に送出する。
 *
 * レースが中止・不成立になった場合、odds3tページ自体は表示される
 * （"データがありません"にはならない）がオッズテーブルが無く、この
 * 例外になる。中止は異常ではなく構造的に避けられない正常系のため、
 * OddsFetchExceptionから分けておき、CaptureOddsJob側でリトライ・
 * failed_jobsへの記録をスキップする判定に使う（2026-10-10、
 * CLAUDE.md「CaptureOddsJobの失敗88件の調査」参照）。
 */
class OddsTableNotFoundException extends OddsFetchException {}
