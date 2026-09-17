"""racer_daily_snapshots から racer_periods を導出するコンパクション処理。

racer_id ごとに observed_date 順で読み、スナップショット値の変化点で
期間を区切る。racer_periods はスナップショット列(valid_to以外)への
UPDATEをDBトリガーで禁止しているため、この処理は:

- まだ存在しない期間 -> INSERT
- 既存期間の末尾(valid_to)を伸縮する必要がある -> UPDATE（valid_toのみ、許可されている）
- 既存期間の valid_from と一致するのに値が食い違う
  -> 「すでに公開済みの期間が最初から間違っていた」ケースであり、
     自動修正せず CompactionError を送出する（手動レビュー対象）
- 期(period_key)をまたがずに値が変化した -> 警告ログ（例外にはしない。
  級別・勝率は本来 前期/後期 の境目でしか変わらないはずだが、
  訂正等で期中に変わることもあり得るため）

--rebuild 指定時は対象 racer の racer_periods を全削除してから
作り直す。race_entries.racer_period_id が既存行を参照している場合は
restrictOnDelete により削除が失敗する（意図的な安全装置）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Iterable

import psycopg

logger = logging.getLogger(__name__)

from ml.loaders.racer_snapshots import period_key_for


class CompactionError(ValueError):
    """既存の racer_periods と computed な期間が矛盾する場合に送出する。"""


@dataclass(frozen=True)
class _ComputedPeriod:
    valid_from: date
    valid_to: date | None
    values: tuple


@dataclass(frozen=True)
class _ExistingPeriod:
    id: int
    valid_from: date
    valid_to: date | None
    values: tuple


@dataclass(frozen=True)
class RacerCompactionResult:
    inserted: int = 0
    updated: int = 0
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class CompactionSummary:
    racers_processed: int
    inserted: int
    updated: int
    warnings: list[str] = field(default_factory=list)


# 期間の区切り判定に使うカラムの明示的なホワイトリスト。
# racer_periods の全カラムを暗黙的に比較するのではなく、ここに列挙した
# カラムの「値が変われば期間を区切る」（新しいracer_periods行を作る）。
# 新しいカラムを racer_periods / racer_daily_snapshots に追加しても、
# ここに追記しない限り区切り判定には使われない（例: weight はレースごとに
# 変動しうるため意図的に含めていない）。
_PERIOD_TRACKED_COLUMNS = (
    "racer_class",
    "branch",
    "national_win_rate",
    "national_win_rate_2",
    "local_win_rate",
    "local_win_rate_2",
)

# 「期(period_key: 前期/後期)をまたがずに変わったら異常」として警告する対象カラム。
# racer_class(級別)は期の境界でのみ更新されるはずで、branch(支部)も頻繁には
# 変わらない。一方で勝率(national/local win rate)は直近成績を反映して随時
# 更新される正常な値であり、期をまたがず変化しても異常ではないため
# _PERIOD_TRACKED_COLUMNS には含めても警告対象からは外す
# （CLAUDE.md 設計上の絶対ルール1）。
_PERIOD_KEY_STABLE_COLUMNS = ("racer_class", "branch")
_PERIOD_KEY_STABLE_INDICES = tuple(
    _PERIOD_TRACKED_COLUMNS.index(col) for col in _PERIOD_KEY_STABLE_COLUMNS
)


def _compute_periods(rows: list[tuple]) -> tuple[list[_ComputedPeriod], list[str]]:
    """observed_date昇順の (observed_date, *values) 行から期間列を作る。"""
    warnings: list[str] = []
    periods: list[_ComputedPeriod] = []

    current_start = rows[0][0]
    current_values = rows[0][1:]
    current_period_key = period_key_for(current_start)

    for observed_date, *values in rows[1:]:
        values = tuple(values)
        if values != current_values:
            new_period_key = period_key_for(observed_date)
            stable_changed = any(
                current_values[i] != values[i] for i in _PERIOD_KEY_STABLE_INDICES
            )
            if new_period_key == current_period_key and stable_changed:
                warnings.append(
                    f"racer_class/branch changed within the same period ({new_period_key}) "
                    f"on {observed_date} (expected only at period boundaries)"
                )
            periods.append(_ComputedPeriod(current_start, observed_date, current_values))
            current_start = observed_date
            current_values = values
            current_period_key = new_period_key

    periods.append(_ComputedPeriod(current_start, None, current_values))
    return periods, warnings


def _fetch_daily_snapshot_rows(cur: psycopg.Cursor, racer_id: int) -> list[tuple]:
    cur.execute(
        f"""
        SELECT observed_date, {", ".join(_PERIOD_TRACKED_COLUMNS)}
        FROM racer_daily_snapshots
        WHERE racer_id = %s
        ORDER BY observed_date
        """,
        (racer_id,),
    )
    return cur.fetchall()


def _fetch_existing_periods(cur: psycopg.Cursor, racer_id: int) -> list[_ExistingPeriod]:
    cur.execute(
        f"""
        SELECT id, valid_from, valid_to, {", ".join(_PERIOD_TRACKED_COLUMNS)}
        FROM racer_periods
        WHERE racer_id = %s
        ORDER BY valid_from
        """,
        (racer_id,),
    )
    # valid_from/valid_to は timestamptz で返るため、observed_date(date型)との
    # 比較・辞書キー一致のために date に正規化する（DB接続のtimezoneはJSTに設定済み）。
    return [
        _ExistingPeriod(
            id=row[0],
            valid_from=row[1].date(),
            valid_to=row[2].date() if row[2] is not None else None,
            values=tuple(row[3:]),
        )
        for row in cur.fetchall()
    ]


def _compact_one_racer(conn: psycopg.Connection, racer_id: int) -> RacerCompactionResult:
    with conn.cursor() as cur:
        rows = _fetch_daily_snapshot_rows(cur, racer_id)
        if not rows:
            return RacerCompactionResult()

        computed_periods, warnings = _compute_periods(rows)
        existing_periods = _fetch_existing_periods(cur, racer_id)
        existing_by_start = {p.valid_from: p for p in existing_periods}
        computed_starts = {p.valid_from for p in computed_periods}

        for existing in existing_periods:
            if existing.valid_from not in computed_starts:
                warnings.append(
                    f"racer_id={racer_id}: existing racer_periods row "
                    f"valid_from={existing.valid_from} has no matching computed period; "
                    "leaving it untouched"
                )

        inserted = 0
        updated = 0

        for cp in computed_periods:
            existing = existing_by_start.get(cp.valid_from)

            if existing is None:
                cur.execute(
                    f"""
                    INSERT INTO racer_periods (
                        racer_id, valid_from, valid_to, {", ".join(_PERIOD_TRACKED_COLUMNS)},
                        created_at, updated_at
                    )
                    VALUES (%s, %s, %s, {", ".join(["%s"] * len(_PERIOD_TRACKED_COLUMNS))}, now(), now())
                    """,
                    (racer_id, cp.valid_from, cp.valid_to, *cp.values),
                )
                inserted += 1
                continue

            if existing.values != cp.values:
                raise CompactionError(
                    f"racer_id={racer_id}: existing racer_periods row (id={existing.id}, "
                    f"valid_from={cp.valid_from}) has values {existing.values} but "
                    f"racer_daily_snapshots computes {cp.values} for the same start date. "
                    "This looks like a correction to an already-published period and must "
                    "be resolved manually (the snapshot columns are immutable by design)."
                )

            if existing.valid_to != cp.valid_to:
                cur.execute(
                    "UPDATE racer_periods SET valid_to = %s, updated_at = now() WHERE id = %s",
                    (cp.valid_to, existing.id),
                )
                updated += 1

    for w in warnings:
        logger.warning("racer_id=%s: %s", racer_id, w)

    return RacerCompactionResult(inserted=inserted, updated=updated, warnings=warnings)


def compact_racer_periods(
    conn: psycopg.Connection,
    *,
    racer_ids: Iterable[int] | None = None,
    rebuild: bool = False,
) -> CompactionSummary:
    with conn.cursor() as cur:
        if racer_ids is None:
            cur.execute("SELECT DISTINCT racer_id FROM racer_daily_snapshots ORDER BY racer_id")
            target_ids = [row[0] for row in cur.fetchall()]
        else:
            target_ids = list(racer_ids)

        if rebuild:
            if target_ids:
                cur.execute(
                    "DELETE FROM racer_periods WHERE racer_id = ANY(%s)",
                    (target_ids,),
                )

    try:
        inserted = updated = 0
        warnings: list[str] = []
        for racer_id in target_ids:
            result = _compact_one_racer(conn, racer_id)
            inserted += result.inserted
            updated += result.updated
            warnings.extend(result.warnings)
    except Exception:
        conn.rollback()
        raise

    conn.commit()
    return CompactionSummary(
        racers_processed=len(target_ids),
        inserted=inserted,
        updated=updated,
        warnings=warnings,
    )
