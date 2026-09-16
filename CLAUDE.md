# BoatRacingWeb

競艇（ボートレース）の着順予測アプリ。

## 構成
- `/` … Laravel 13（API・管理画面・オッズ取得バッチ）
- `/ml` … Python 3.12 + uv（取込・特徴量・学習・推論）
- DB: PostgreSQL 18 / boat_racing_db

## コマンド
- Python側は必ず `uv run` 経由（`cd ml && uv run python ...`）
- `pip` を直接使わない。依存追加は `uv add`
- システムPython（3.14）は触らない

## 設計上の絶対ルール
1. **リーク防止**：特徴量は「配信時刻(締切10分前)までに確定した情報」のみ。
   選手の級別・勝率は racer_periods の期間スナップショットから引く。
2. **時系列split**：学習・検証でランダムKFoldは禁止。日付カットオフで分割。
3. `lane`（枠番）と `start_course`（進入コース）は別物。混同しない。
4. `st` は符号付き（フライングは負値）。`finish_pos` は失格時 null。
5. `predictions` は published_at 以降イミュータブル。更新禁止。

## プロダクト方針
- 高オッズ狙いではなく的中率・確率精度で勝負する
- 主要指標は Brier score / log loss。accuracy は副次
- 的中率と回収率は必ず並記する
