"""特徴量生成 第5層: 直前情報(beforeinfo)由来の展示・気象特徴量。

feature_version = 'v5_exhibition'

CLAUDE.md ルール1（リーク防止）:
- captured_atは「取得した時刻」であって「サイトが公開した時刻」ではない。
  race_before_info.source で取得経路を区別し、検証方法を分岐する
  (2026-09-27、v5生成時に発覚。詳細はCLAUDE.md参照):
  - source='live'（締切T-12分の予約ジョブでの取得）: captured_atは実際の
    公開時刻に近いため、従来通り captured_at <= cutoff_at を
    機械的に検証する(ml.cutoff.assert_no_leak)。1件でも違反があれば
    FeatureGenerationErrorで停止する。
  - source='backfill'（過去分を後日まとめて取得。現在の3ヶ月分は全件これ）:
    captured_atは取得作業を行った日時になり、対象レースの締切よりずっと
    後になるため、この比較自体が無意味。代わりに「beforeinfoは締切の
    T-14〜16分に公開され、締切を過ぎてもページの内容は変わらない」という
    サイト挙動(CLAUDE.md「直前情報(beforeinfo)の取得」で実測・確認済み)を
    根拠とする。「データがありません」にならず値が取得できている時点で、
    その内容は締切前に公開されていたものだと保証されるため、
    captured_atによる検証は行わない。
  - race_weather_infoにはsource列を追加していない。天候は艇情報と同じ
    ページ・同じ取得タイミングで得られる(1回のページ取得でboats+weatherを
    同時に得る)ため、同一レースのrace_before_info.sourceで代表させる。
- race_before_info/race_weather_infoが記録されているのは現時点で
  2026-06-21〜2026-09-20のみ（財団への利用許諾確認のため自動取得は
  一時停止中。CLAUDE.md「直前情報(beforeinfo)の取得」参照）。対象期間外の
  レースはINNER JOINにより自然に除外される。

特徴量:
- exhibit_time / そのレース内順位(1=最速) / レース平均との差
- st_exhibit / そのレース内順位(1=最速)
- course_predicted（展示での進入コース、実際のレースの進入コースではない）
- tilt, exhibit_weight, exhibit_adjusted_weight（直前計量の実測値。
  v1_basic.weightは番組表発表時点の公表体重で別物のため、同名衝突を避けて
  exhibit_接頭辞を付ける）
- propeller_changed_flag（プロペラ交換の有無、0/1）
- parts_exchanged_flag（部品交換の有無、0/1。交換内容の種類までは見ない）
- 気象6項目: temperature, wind_speed, wind_direction_code, wave_height,
  water_temperature, weather_condition_code
  - weather_conditionは「晴/曇り/雨」等の文字列のため、学習用に固定の
    整数コードへ変換する(WEATHER_CONDITION_CODES)。未知の値は欠損(None)
    として扱い、当て推量のコードは割り当てない。
  - wind_direction_codeは実際の方角との対応が未確定(CLAUDE.md参照)の
    ままDBに保持されている値をそのまま使う。順序に意味がある保証がない
    ため、学習側ではcategorical_featureとして扱うこと。

順位・平均差はレースごとに最大6艇分をPython側でまとめて計算する
（v2_recent/v4_stadiumのような大規模expanding windowと違い対象は
3ヶ月分・数万行程度のため、SQLのウィンドウ関数でNULLの扱いを複雑にする
より単純で読みやすい）。
"""

from __future__ import annotations

import argparse
import time
from dataclasses import dataclass
from datetime import date, timedelta

import psycopg
from psycopg.types.json import Jsonb

from ml.cutoff import CutoffViolationError, assert_no_leak
from ml.loaders.db import get_connection

FEATURE_VERSION = "v5_exhibition"
CUTOFF_MARGIN = timedelta(minutes=10)
DEFAULT_BATCH_SIZE = 2000

# 実データで観測された値(2026-06-21〜09-20の3ヶ月分)のみを明示的にマッピングする。
# 未知の値が来た場合は当て推量せずNoneにする。
WEATHER_CONDITION_CODES = {"晴": 0, "曇り": 1, "雨": 2}


class FeatureGenerationError(ValueError):
    """特徴量生成に失敗した場合に送出する。"""


@dataclass(frozen=True)
class FeatureGenerationResult:
    start_date: date
    end_date: date
    row_count: int
    upserted: int
    elapsed_seconds: float


# race_ids_clauseはstage2用(--race-ids)でのみ埋める。省略時(日付範囲のみ、
# バックフィル・過去分の一括生成で使う既存挙動)は空文字のまま変化なし。
_SELECT_SQL = """
    SELECT
        re.race_id, re.lane, r.deadline_at,
        rbi.exhibit_time, rbi.st_exhibit, rbi.course_predicted, rbi.tilt,
        rbi.weight, rbi.adjusted_weight, rbi.propeller_changed, rbi.parts_exchanged,
        rbi.captured_at AS boat_captured_at, rbi.source,
        rwi.temperature, rwi.wind_speed, rwi.wind_direction_code, rwi.wave_height,
        rwi.water_temperature, rwi.weather_condition, rwi.captured_at AS weather_captured_at
    FROM race_entries re
    JOIN races r ON r.id = re.race_id
    JOIN race_before_info rbi ON rbi.race_id = re.race_id AND rbi.lane = re.lane
    JOIN race_weather_info rwi ON rwi.race_id = re.race_id
    WHERE r.race_date BETWEEN %(start_date)s AND %(end_date)s
    {race_ids_clause}
    ORDER BY re.race_id, re.lane
"""


def _rank_ascending(values: dict[int, float]) -> dict[int, int]:
    """{lane: value} から {lane: レース内順位(1=最小値、同値はRANK()と同じ飛び番)} を返す。

    valuesにはNoneでない値のみを渡すこと（呼び出し側でフィルタ済みの前提）。
    """
    ordered = sorted(values.items(), key=lambda item: item[1])
    ranks: dict[int, int] = {}
    for i, (lane, value) in enumerate(ordered):
        if i > 0 and value == ordered[i - 1][1]:
            ranks[lane] = ranks[ordered[i - 1][0]]
        else:
            ranks[lane] = i + 1
    return ranks


def _build_race_payloads(rows: list[tuple]) -> list[tuple[int, int, object, dict]]:
    """1レース分(最大6行)ずつ処理し、(race_id, lane, cutoff_at, payload)のリストを返す。

    行はrace_id, lane昇順で渡されている前提（呼び出し側のSQLでORDER BY済み）。
    """
    results: list[tuple[int, int, object, dict]] = []

    i = 0
    n = len(rows)
    while i < n:
        race_id = rows[i][0]
        j = i
        group: list[tuple] = []
        while j < n and rows[j][0] == race_id:
            group.append(rows[j])
            j += 1

        exhibit_times = {r[1]: float(r[3]) for r in group if r[3] is not None}
        st_exhibits = {r[1]: float(r[4]) for r in group if r[4] is not None}
        avg_exhibit_time = (
            sum(exhibit_times.values()) / len(exhibit_times) if exhibit_times else None
        )
        exhibit_time_ranks = _rank_ascending(exhibit_times)
        st_exhibit_ranks = _rank_ascending(st_exhibits)

        for row in group:
            (
                _race_id, lane, deadline_at,
                exhibit_time, st_exhibit, course_predicted, tilt,
                weight, adjusted_weight, propeller_changed, parts_exchanged,
                boat_captured_at, source,
                temperature, wind_speed, wind_direction_code, wave_height,
                water_temperature, weather_condition, weather_captured_at,
            ) = row

            cutoff_at = deadline_at - CUTOFF_MARGIN

            if source == "live":
                # ライブ取得はcaptured_atが実際の公開時刻に近いため、従来通り
                # 機械的に検証する。
                for captured_at, label in (
                    (boat_captured_at, "race_before_info"),
                    (weather_captured_at, "race_weather_info"),
                ):
                    try:
                        assert_no_leak(
                            [{"captured_at": captured_at}],
                            cutoff_at=cutoff_at,
                            time_field="captured_at",
                            label=f"{label}(race_id={race_id}, lane={lane})",
                        )
                    except CutoffViolationError as exc:
                        raise FeatureGenerationError(str(exc)) from exc
            elif source == "backfill":
                # captured_atは取得作業を行った日時であり検証の根拠にならない。
                # 「beforeinfoは締切T-14〜16分に公開され、締切後も内容が変わらない」
                # というサイト挙動(CLAUDE.md実測済み)を根拠として、captured_atでの
                # 検証はスキップする。「データがありません」にならず値が取れている
                # 時点で、その内容は締切前に公開されていたものだと保証される。
                pass
            else:
                raise FeatureGenerationError(
                    f"race_before_info(race_id={race_id}, lane={lane}): "
                    f"unknown source={source!r} (expected 'live' or 'backfill')"
                )

            exhibit_time_f = float(exhibit_time) if exhibit_time is not None else None
            payload = {
                "exhibit_time": exhibit_time_f,
                "exhibit_time_rank_in_race": exhibit_time_ranks.get(lane),
                "exhibit_time_dev_from_race_avg": (
                    exhibit_time_f - avg_exhibit_time
                    if exhibit_time_f is not None and avg_exhibit_time is not None
                    else None
                ),
                "st_exhibit": float(st_exhibit) if st_exhibit is not None else None,
                "st_exhibit_rank_in_race": st_exhibit_ranks.get(lane),
                "course_predicted": course_predicted,
                "tilt": float(tilt) if tilt is not None else None,
                # v1_basic.weightは番組表発表時点の公表体重で、こちらは直前計量の
                # 実測値。異なる情報のためexhibit_接頭辞で明確に区別する
                # (同名"weight"で衝突しpolars/LightGBMに片方しか渡らなくなる
                # バグを2026-09-27に発見・修正)。
                "exhibit_weight": float(weight) if weight is not None else None,
                "exhibit_adjusted_weight": (
                    float(adjusted_weight) if adjusted_weight is not None else None
                ),
                "propeller_changed_flag": int(bool(propeller_changed)),
                "parts_exchanged_flag": int(bool(parts_exchanged)),
                "weather_temperature": float(temperature) if temperature is not None else None,
                "weather_wind_speed": float(wind_speed) if wind_speed is not None else None,
                "weather_wind_direction_code": wind_direction_code,
                "weather_wave_height": float(wave_height) if wave_height is not None else None,
                "weather_water_temperature": (
                    float(water_temperature) if water_temperature is not None else None
                ),
                "weather_condition_code": WEATHER_CONDITION_CODES.get(weather_condition),
            }

            results.append((race_id, lane, cutoff_at, payload))

        i = j

    return results


def _bulk_upsert(cur: psycopg.Cursor, batch: list[tuple]) -> None:
    if not batch:
        return
    values_sql = ", ".join(["(%s, %s, %s, %s, %s, now(), now())"] * len(batch))
    params: list[object] = []
    for race_id, lane, cutoff_at, payload in batch:
        params.extend([race_id, lane, FEATURE_VERSION, cutoff_at, Jsonb(payload)])

    cur.execute(
        f"""
        INSERT INTO features (race_id, lane, feature_version, cutoff_at, payload,
                               created_at, updated_at)
        VALUES {values_sql}
        ON CONFLICT (race_id, lane, feature_version) DO UPDATE SET
            cutoff_at = EXCLUDED.cutoff_at,
            payload = EXCLUDED.payload,
            updated_at = now()
        """,
        params,
    )


def generate_exhibition_features_range(
    conn: psycopg.Connection,
    start_date: date,
    end_date: date,
    *,
    race_ids: list[int] | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    progress: bool = False,
) -> FeatureGenerationResult:
    """race_ids を指定すると、日付範囲の中でもそのrace_idだけに絞る
    (stage2用。省略時は日付範囲の全レースが対象で、既存のバックフィル/
    過去分一括生成の挙動と完全に同じ)。
    """
    t0 = time.time()

    params: dict[str, object] = {"start_date": start_date, "end_date": end_date}
    race_ids_clause = ""
    if race_ids:
        race_ids_clause = "AND re.race_id = ANY(%(race_ids)s)"
        params["race_ids"] = list(race_ids)
    sql = _SELECT_SQL.format(race_ids_clause=race_ids_clause)

    with conn.cursor() as read_cur:
        read_cur.execute(sql, params)
        raw_rows = read_cur.fetchall()

    if progress:
        print(f"  fetched {len(raw_rows)} rows ({time.time() - t0:.1f}s)", flush=True)

    payloads = _build_race_payloads(raw_rows)

    batch: list[tuple] = []
    upserted = 0

    with conn.cursor() as write_cur:
        for race_id, lane, cutoff_at, payload in payloads:
            batch.append((race_id, lane, cutoff_at, payload))

            if len(batch) >= batch_size:
                _bulk_upsert(write_cur, batch)
                conn.commit()
                upserted += len(batch)
                batch.clear()
                if progress:
                    print(
                        f"  progress: {upserted} rows upserted ({time.time() - t0:.1f}s)",
                        flush=True,
                    )

        if batch:
            _bulk_upsert(write_cur, batch)
            conn.commit()
            upserted += len(batch)

    elapsed = time.time() - t0
    return FeatureGenerationResult(
        start_date=start_date,
        end_date=end_date,
        row_count=len(payloads),
        upserted=upserted,
        elapsed_seconds=elapsed,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("start", help="YYYY-MM-DD")
    parser.add_argument(
        "end", nargs="?", default=None, help="YYYY-MM-DD（省略時はstartと同じ=1日分）"
    )
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--progress", action="store_true")
    parser.add_argument("--show", type=int, default=3)
    parser.add_argument(
        "--race-ids",
        type=int,
        nargs="+",
        default=None,
        help="指定したrace_idのみ対象にする(省略時は日付範囲の全レース)。stage2用",
    )
    args = parser.parse_args(argv)

    start_date = date.fromisoformat(args.start)
    end_date = date.fromisoformat(args.end) if args.end else start_date

    conn = get_connection()
    try:
        result = generate_exhibition_features_range(
            conn,
            start_date,
            end_date,
            race_ids=args.race_ids,
            batch_size=args.batch_size,
            progress=args.progress,
        )
        print(
            f"{result.start_date}..{result.end_date}: rows={result.row_count} "
            f"upserted={result.upserted} elapsed={result.elapsed_seconds:.1f}s "
            f"feature_version={FEATURE_VERSION}"
        )

        if args.show:
            import json

            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT race_id, lane, cutoff_at, payload
                    FROM features f
                    JOIN races r ON r.id = f.race_id
                    WHERE r.race_date BETWEEN %s AND %s AND f.feature_version = %s
                    ORDER BY race_id, lane
                    LIMIT %s
                    """,
                    (start_date, end_date, FEATURE_VERSION, args.show),
                )
                for race_id, lane, cutoff_at, payload in cur.fetchall():
                    print(f"--- race_id={race_id} lane={lane} cutoff_at={cutoff_at} ---")
                    print(json.dumps(payload, ensure_ascii=False, indent=2))
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
