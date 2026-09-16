"""特徴量生成のカットオフ検証テスト雛形。

CLAUDE.md ルール1（リーク防止）: 特徴量は配信時刻(締切10分前)までに
確定した情報のみを使ってよい。カットオフ後のオッズ・結果が
混入した場合は、フィルタして黙って続行するのではなく fail する
ことをここで固定する。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from ml.cutoff import CutoffViolationError, assert_no_leak

CUTOFF_AT = datetime(2026, 9, 17, 11, 50, tzinfo=timezone.utc)  # deadline_at(12:00) - 10min 相当


def test_odds_snapshots_before_cutoff_pass() -> None:
    rows = [
        {"captured_at": CUTOFF_AT - timedelta(minutes=30)},
        {"captured_at": CUTOFF_AT},  # ちょうどcutoffは許容
    ]

    assert_no_leak(rows, cutoff_at=CUTOFF_AT, time_field="captured_at", label="odds_snapshots")


def test_odds_snapshots_after_cutoff_mixed_in_fails() -> None:
    rows = [
        {"captured_at": CUTOFF_AT - timedelta(minutes=30)},
        {"captured_at": CUTOFF_AT + timedelta(seconds=1)},  # カットオフ後に混入したオッズ
    ]

    with pytest.raises(CutoffViolationError):
        assert_no_leak(rows, cutoff_at=CUTOFF_AT, time_field="captured_at", label="odds_snapshots")


def test_race_results_before_cutoff_pass() -> None:
    rows = [
        {"created_at": CUTOFF_AT - timedelta(hours=1)},
    ]

    assert_no_leak(rows, cutoff_at=CUTOFF_AT, time_field="created_at", label="race_results")


def test_race_results_after_cutoff_mixed_in_fails() -> None:
    # レース結果はカットオフ時点ではまだ確定していないはずのデータ。
    # 何らかの原因で結果確定後の行が特徴量生成に混入した場合を再現する。
    rows = [
        {"created_at": CUTOFF_AT - timedelta(hours=1)},
        {"created_at": CUTOFF_AT + timedelta(minutes=5)},  # レース結果の混入
    ]

    with pytest.raises(CutoffViolationError):
        assert_no_leak(rows, cutoff_at=CUTOFF_AT, time_field="created_at", label="race_results")


def test_naive_datetime_is_rejected() -> None:
    rows = [{"captured_at": datetime(2026, 9, 17, 11, 0)}]  # tz情報なし

    with pytest.raises(ValueError):
        assert_no_leak(rows, cutoff_at=CUTOFF_AT, time_field="captured_at", label="odds_snapshots")
