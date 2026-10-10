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
   - 級別(racer_class)は期別（前期/後期）の境界でのみ更新される。
   - 期境界は1月/7月（前期=1〜6月、後期=7〜12月）。当初5月/11月と想定していたが、
     racer_daily_snapshots の実データでracer_classの変化日を集計したところ1月・7月に
     9割以上が集中しており（5月・11月付近はほぼ0件）、誤りと判明したため修正した
     （2026-09-18、`ml/src/ml/loaders/racer_snapshots.py` の `period_key_for()`）。
   - 改定日自体は各月の月初だが、番組表(B)への反映はその選手が次に出走した日になる。
     そのため変化の「観測日」は1月・7月の上旬(概ね1〜14日目)に分散する。1日等の
     単一日に鋭く集中するわけではないので、日付が揃っていないことを異常と誤解しないこと。
   - 勝率(national/local)は期境界と無関係に、直近成績を反映して随時更新される。当地勝率
     (local_win_rate系)は出走場によっても変動する。
   - そのため racer_periods の期間は級別の期境界だけでなく、勝率更新のタイミングでも区切られる。1つの級別期間内に複数の racer_periods 行が存在するのは正常であり、バグではない。
2. **時系列split**：学習・検証でランダムKFoldは禁止。日付カットオフで分割。
3. `lane`（枠番）と `start_course`（進入コース）は別物。混同しない。
4. `st` は符号付き（フライングは負値）。`finish_pos` は失格時 null。
5. `predictions` は published_at 以降イミュータブル。更新禁止。

## タイムゾーン
- `app.timezone` は UTC、DBセッションもUTCで統一する。
- ただし「開催日」の判定は必ず Asia/Tokyo で行う。UTCの日付で判定すると09:00 JSTより前は前日扱いになる。
- レース関連の日付（race_date等）はJSTの暦日であり、UTCの暦日ではない。`now()->toDateString()` のような app.timezone(UTC) 依存の日付取得を「本日の開催日」の判定に使わない。

## プロダクト方針
- 高オッズ狙いではなく的中率・確率精度で勝負する
- 主要指標は Brier score / log loss。accuracy は副次
- 的中率と回収率は必ず並記する

## 確率の特性（2026-09-19 検証）
- 現行モデル(v3_binary)の p_first は、高確率帯(50%以上)で
  系統的に弱気。予測65%→実際69%、予測73%→実際78.5%
- 90%以上の予測は出力されない（レース内正規化の構造上の上限）
- Isotonic補正を試したが改善は僅少(log loss -0.0004)のため未導入。
  実装は ml/src/ml/models/calibration.py に残してある
- 買い目の点数決定でこの傾向を考慮すること。
  累積確率ベースで点数を決めると、過小評価分だけ
  余計に買うことになる

## p_top3の精度検証（2026-09-21）
- p_first とは逆の傾向: p_top3は**低確率帯で弱気（実際はもっと高い）、
  高確率帯で強気（実際はもっと低い）**。10分位で最大+9.6pt(低確率帯)〜
  -13.2pt(83〜95%帯)のズレ。p_firstの「高確率帯で弱気」とは逆方向なので
  混同しないこと。
- p_top3 >= X%で絞っても**90%を超える実績には届かない**
  （X=90%でも実際3着以内率86.38%が上限、検証期間40,214レース）。
  「p_top3が高い」＝「ほぼ確実に3着以内」と読むのは禁物。
- 1号艇の実際3着以内率（ベースライン）: 80.90%
- 予測top3(3艇)のうち平均2.08艇/3艇が実際も3着以内（艇単位分類的中率
  69.24%）。完全一致(3/3)は24%のレースのみ。
## 回収率の検証結果（2026-09-19）
- 検証期間(2026-01-01〜09-17)の40,692レース、341,630チケットで検証
- 全体回収率 75.01%（控除率25%とほぼ一致）
- エントロピー分位別: 的中率は81%→32%と変動するが、
  回収率は70〜80%のレンジで横ばい。相関なし
- 月別も69.7〜79.3%で安定
- 結論: 市場は効率的で、確率推定の精度だけでは
  控除率を超えられない。プロダクトは「的中率」を
  訴求し、回収率は必ず併記する

## 1号艇危険察知（2026-09-19 検証）
- モデルが1号艇以外を推奨したレース(全体の10.6%)では、
  1号艇の的中率が 54.95% → 22.35% に低下。本物の予測力
- 同一レース群でモデルの選択は1号艇を買うより +8.26pt
- p_first(lane=1) < X% のレースを除外して1号艇を買う戦略:
  X=30で回収率 89.68% → 90.95%、X=50で 91.40%
  学習期間でも同方向に再現（92.88〜93.59%）
- ただし100%には届かない。長期的には負ける戦略である点は変わらない
- 分位別の非対称性: 低確率帯は予測より実際がさらに弱く(-4.53pt)、
  高確率帯は実際が強い(+6.24pt)

## motor_win_rate_2パースバグとモデル切替（2026-09-21）
- `ml/src/ml/parsers/program.py` の `_OFF_MOTOR_WIN_RATE_2` が
  生バイト位置で1バイトずれていた（`slice(51,55)` → 正しくは
  `slice(50,55)`）。10%以上の値は十の位が欠落し（例: 29.17→9.17）、
  10%未満の値は右詰め用の空白が消えるだけで結果的に無傷だったため、
  既存テスト(1桁の値のみ検証)ではバグを検出できなかった。
  2桁のケースの回帰テストを `tests/test_program_parser.py` に追加済み。
- 影響: race_entries.motor_win_rate_2 の 974,418/1,025,280行(95.0%)
  が実際に値が変わった。修正後の分布(median 33.33)はboat_win_rate_2
  とほぼ同形状になり妥当性を確認。v1_basic特徴量を全期間再生成
  （v2_recent/v3_relativeはmotor_win_rate_2を直接参照しないため
  再生成不要と確認済み）。
- 新モデル `v3_binary_20260920` を再学習し、本番の
  `PREDICTION_MODEL_VERSION` をこれに切替（旧`v3_binary_20260919`）。
  検証期間: 的中率56.20%→56.87%、回収率75.01%→74.90%、
  log loss 1.19950→1.19744、Brier 0.58425→0.58339（いずれも僅かに改善、
  的中率・回収率はほぼ横ばい）。旧モデルのpredictions/tickets/
  judgmentsはmodel_version違いでそのまま残置し、新モデル分も検証期間
  全体(2026-01-01〜09-17)を並行生成済み。
- 閾値の再較正（config/ml.php）:
  - confidence_thresholds: S/A/B = 0.64/0.69/0.79 → **0.609/0.690/0.773**
    （新モデルの検証期間・正規化エントロピー分布の25/50/75%分位点）
  - lane1_risk_threshold(0.35)・upset_pick_threshold(0.40)は新モデルでも
    該当率がほぼ同水準（1号艇危険13.08%→13.12%、妙味レース4.71%→4.65%）
    だったため据え置き
  - 「1号艇が危険なレース」と「妙味のあるレース」は条件が重複しうる
    （検証期間で1号艇危険の29.5%が妙味にも該当）ため、画面上は
    妙味を優先しlane1_risk側から除外して表示する
    （`resources/js/pages/RacesToday.vue`）

## 直前情報(beforeinfo)の取得（2026-09-21〜）
- URL: `https://www.boatrace.jp/owpc/pc/race/beforeinfo?rno={race_no}&jcd={stadium_code:02d}&hd={Ymd}`
  （odds3tと同じ場コード・日付規則。jcdはstadiums.codeと一致）
- サーバーサイドレンダリングされた静的HTMLで、JS実行なしのcurl/Http::getで
  取得可能（実測・確認済み）。「データがありません」の文言で無効な
  組み合わせ(未開催日等)を判定できる。
- 公開タイミングは**締切のT-14〜16分**（本日2026-09-21、三国1Rで実測）。
  T-12にジョブを予約すれば通常は既に公開済み。
- 締切を過ぎてもページの表示内容は変わらない（着順欄は最後まで空欄で
  別ページ(raceresult)へのリンクのみ）。展示タイム等は締切後も消えない。
- 実装は `app/Services/Boatrace/BeforeInfoScraper.php`（Symfony
  DomCrawlerでパース。TrifectaOddsScraperと同じくrowspanを位置ベースで
  読み取る固定レイアウト前提）、`app/Jobs/CaptureBeforeInfoJob.php`
  （締切T-12にdispatch()->delay()、CaptureOddsJobと同方式）、
  `race_before_info`（艇ごと: 体重/調整重量/展示タイム/チルト/
  プロペラ交換/交換部品/展示スタートのコース・ST）と
  `race_weather_info`（レース単位: 気温/天候/風速/風向コード/水温/波高）
  の2テーブルに保存する。
- 風向はCSSクラス名(`is-windN`)のアイコンでしか表現されておらず、Nと
  方角の対応が未確定なため、コードのまま `wind_direction_code` に
  保持している（方角への変換は別途要検証）。
- 進入予想(`course_predicted`)はスタート展示テーブルの行(=コース)と
  艇番の対応から確定した値で、実際のレースの進入コースではない
  （あくまで展示時点の予想）。
- 過去日分もbeforeinfoページ自体は取得できる（2023-09-01時点のデータも
  実データが返ってきた）が、1リクエストあたり実測**約9.5秒**と非常に
  遅く、全期間(約17万レース)を逐次バックフィルすると計算上
  **約450時間(≒19日)** かかり非現実的と判断。過去分のバックフィルは
  行わず、**本日(2026-09-21)以降のみリアルタイムで記録**する方針とした。
  当面は記録のみで特徴量・学習には使わない。数ヶ月分貯まった時点で
  特徴量に追加することを検討する（`v4`はstadium特徴量が使ったため、
  追加するなら`v5_exhibition`等の名前になる）。

### 現在ステータス: 財団への利用許諾確認のため一時停止中（2026-09-21）
- `https://www.boatrace.jp/owpc/pc/extra/policy.html`（サイトポリシー）
  「禁止事項について」5. に「不正アクセス、大量の情報送受信及び大量の
  アクセスなど、本サイトの運営に支障を与える行為」の禁止規定があり、
  robots.txt（`Disallow:`なし、技術的には全許可）とは別に、この
  利用規約への抵触リスクが未解消のため、一般財団法人BOATRACE振興会に
  確認するまで自動取得を停止した。
  - 停止した作業: 過去データの広範囲サンプリング調査(320件、途中で中断)、
    3ヶ月分(2026-06-21〜09-20、約14,772レース分)のバックフィル計画
    （実行前に停止、未実行）
  - 停止した本番稼働: `routes/console.php` の `beforeinfo:schedule-today`
    スケジュール登録をコメントアウト。キュー済みだった本日分の
    `CaptureBeforeInfoJob` 154件は削除済み（オッズ取得ジョブ308件には
    手を付けていない）
  - コード・テーブル(`race_before_info`/`race_weather_info`)自体は
    削除せず残してある。許諾が取れ次第 `routes/console.php` の
    コメントアウトを解除すれば再開できる
  - 問い合わせ窓口: `https://www.boatrace.jp/owpc/pc/support/opinion`
    （サイトポリシーページ記載の「お問い合わせフォーム」）

### Python版fetch/load/backfill実装（2026-09-23、実行はまだしていない）
- 一時停止中のステータスを維持したまま、PHP版とは別にPython側の実装だけ
  先に進めた（ユーザーの明示判断）。財団からの許諾が取れるまで実際の
  リクエストは送らない。
- `ml/src/ml/fetchers/beforeinfo.py`：HTML取得(`fetch_before_info_html`/
  `fetch_before_info`)とパース(`parse_before_info_html`)を分離。パースは
  ネットワークなしでテスト可能。`BeforeInfoScraper.php`と同じ固定レイアウト
  前提（17列td・`table.is-w238`のコース行対応・`div.weather1`）を移植。
  HTML解析に`beautifulsoup4`+`lxml`を`uv add`で追加（ml側に無かった依存）。
- `ml/src/ml/loaders/beforeinfo.py`：`race_before_info`/`race_weather_info`
  へON CONFLICT upsert。対応する`race_entries`(race_id, lane)が無い場合は
  `LoaderError`を送出し黙って捨てない。
- `ml/src/ml/fetchers/beforeinfo_backfill.py`：`races`を`race_date`降順
  （新しい日付優先）で処理し、JSON状態ファイルで中断・再開、失敗は記録して
  継続。nohupでのバックグラウンド実行前提。
- 動作確認：調査時のbeforeinfo生HTMLは保存しておらず(`/tmp`等に残存なし)
  再取得もしていないため、`ml/tests/_beforeinfo_fixtures.py`の固定HTML
  フィクスチャ（lane1の展示タイム6.97・体重53.3kg・チルト-0.5等は実際の
  調査報告の観測値を採用）でパーサを検証（`test_beforeinfo_parser.py`、
  11件）。DB投入は同フィクスチャの解析結果を使い、実PostgreSQL
  （テスト専用race_date=2099-01-01、テスト終了時にcascade削除）で
  upsert・冪等性・race_entries欠如時の例外を検証（`test_beforeinfo_loader.py`、
  3件）。ml側テスト全53件パス。

### 2026-09-19分の実データ取得と応答時間の訂正（2026-09-27、財団許諾未取得のままユーザー判断でリスク受容し実行）
- 1日分(156レース)を実行し、`race_before_info`936行・`race_weather_info`156行
  を投入。全156件成功（失敗0）。展示タイム中央値6.8秒（分布6.47〜7.09秒）で
  事前予想と一致、体重39.5〜59.1kg、チルト-0.5〜3.0、st_exhibit 0.00〜0.49、
  parts_exchangedは実在の部品名（リング×２/ギヤ/ピストン×２,シャフト等）で
  実データと確認。フィクスチャ前提の17列固定レイアウトはパース例外0件。
- **重要な訂正**：「サーバー応答が遅くこれ自体がレート制御になる」という
  上記(2026-09-21時点)の前提は誤りだったと判明した。1日分バックフィルが
  156件を33秒（平均0.21秒/件）で完了し、当初想定(9.5秒/件)と大きく矛盾した
  ため検証したところ：
  - `curl`で同一URLを再取得: 8〜10秒（従来の実測値と一致、HTTP/1.1強制でも
    10.16秒・HTTP/2で8.49秒なのでHTTP/2起因ではない）
  - 本スクリプトが使うPython `urllib.request` 経由（プロジェクトコードから
    独立に検証）: 同一URL・同一内容が0.2〜0.3秒で返る
  - 取得内容自体はcurl/urllibで完全一致（実際の値を相互確認済み）しており
    データの正当性に問題はない
  - 原因不明のツール依存差（TLSフィンガープリント差など未特定）。
    **サーバー自体は速く**、「応答の遅さ＝事実上のレート制御」という前提は
    崩れた。明示的なsleepなしでは短時間に大量リクエストを送る形になり、
    「大量アクセス」リスクをむしろ高める
  - 対策として`beforeinfo_backfill`の`--sleep-seconds`既定値を**0秒→2.0秒**
    に変更（2026-09-27）
- **所要時間の再計算**（sleep=2.0秒 + 実測fetch時間0.2〜0.3秒/件、
  1件あたり計約2.2〜2.3秒として計算）:
  - 3ヶ月分(2026-06-21〜09-20、実データ14,772件): **約9.0〜9.4時間**
    （旧見積り: 9.5秒/件換算で約39時間だったので、旧見積りより大幅に短縮）
  - 参考: 全期間(2023-09-01〜2026-09-20、170,880件)なら**約4.4〜4.6日**
    （旧見積りの「約450時間(≒19日)」から大幅に短縮。ただし全期間実行は
    別途判断が必要、現時点では3ヶ月分のみが検討対象）

### 3ヶ月分バックフィル完了（2026-06-21〜09-20、2026-09-27）
- 対象14,772レース全件成功。`race_before_info` 88,632行(14,772×6)、
  `race_weather_info` 14,772行を投入し、件数・カバレッジとも欠落なしで一致
  確認済み（sleep=2.0秒、所要9時間35分）。
- 実行中に1件、新種のパースバグを発見・修正した。`jcd=23,rno=4,hd=20260622`
  で`ValueError("invalid literal for int() with base 10: ''")`が発生。原因は
  スタート展示テーブル(`table.is-w238`)で、枠が展示不参加(欠場等)の場合に
  艇番spanの要素自体は残り中身が`&nbsp;`(`\xa0`)のみになるケースがあり、
  フィクスチャはこれを想定していなかったため。`_merge_exhibit_start()`で
  nbsp除去後に空文字なら「このコースに艇なし」として安全にスキップする
  よう修正(`ml/src/ml/fetchers/beforeinfo.py`)。回帰テストを
  `test_beforeinfo_parser.py::test_exhibit_absent_lane_leaves_nbsp_only_span_without_raising`
  に追加、`--retry-failed`で26件(この1件+一時的なDNS解決失敗25件)を
  再取得し全件成功。ml側テスト全54件パス。
- 本番モデルの特徴量・学習にはまだ使わない（記録のみの方針は継続、
  上記「直前情報(beforeinfo)の取得」の当初方針どおり）。

### captured_at の意味とsource列の追加（2026-09-27、v5_exhibition生成時に発覚）
- `race_before_info.captured_at` は「取得した時刻」であって「サイトが公開した
  時刻」ではない。ライブ取得(締切T-12分の予約ジョブ)ではこの2つはほぼ一致
  するが、バックフィル(過去分を後日まとめて取得)ではcaptured_atが取得作業を
  行った日時になり、対象レースの締切よりずっと後になる。実際、上記3ヶ月分
  バックフィルの88,632行全件で`captured_at > deadline_at - 10分`となって
  いた（v5_exhibition特徴量のリーク検証で発覚。1件だけの偶然ではなく
  全件がこの状態だった）。
- `race_before_info` に `source` 列(`'live'` / `'backfill'`)を追加し
  （migration: `2026_09_27_100001_add_source_to_race_before_info_table.php`）、
  取得経路を明示的に区別できるようにした。既存行のうち3ヶ月分の88,632行は
  `'backfill'`。もともと財団確認待ちで一時停止する直前に記録されていた
  6行(race_id=185402、2026-09-21分)は、実際にはライブ取得(締切T-12分の
  予約ジョブ)によるcaptured_atが締切の約10分16秒前という妥当な値だった
  ため、`'live'`に修正済み。`app/Jobs/CaptureBeforeInfoJob.php`（PHP、
  ライブ取得経路）と`ml/src/ml/loaders/beforeinfo.py::load_before_info()`
  （Python、`source`を必須キーワード引数化、backfillスクリプト側で
  `source="backfill"`を明示）の両方で、以後は必ずsourceを明示する。
- リーク検証(`ml/src/ml/features/exhibition.py`)はsourceで分岐する:
  - `source='live'`: 従来通り`captured_at <= cutoff_at(=deadline_at-10分)`
    を機械的に検証する(`ml.cutoff.assert_no_leak`)。
  - `source='backfill'`: captured_atでの検証はスキップする。根拠は
    captured_atではなく「beforeinfoは締切T-14〜16分に公開され、締切後も
    ページの内容が変わらない」という実測済みのサイト挙動そのもの
    （上記「直前情報(beforeinfo)の取得」参照）。「データがありません」に
    ならず値が取得できている時点で、その内容は締切前に公開されていた
    ものだと保証される、という論拠。
  - `race_weather_info`にはsource列を追加していない。天候は艇情報と同じ
    ページ・同じ取得タイミングで得られるため、同一レースの
    `race_before_info.source`で代表させる。
- 今後ライブ取得(`beforeinfo:schedule-today`)を財団許諾後に再開すれば、
  以降に記録される行は`source='live'`となり、厳格なcaptured_at検証が
  自動的に効くようになる。

## v5_exhibition 特徴量（2026-09-27）
- 直前情報(beforeinfo)由来の展示・気象特徴量。`ml/src/ml/features/exhibition.py`。
  展示タイム/そのレース内順位/レース平均との差、st_exhibit/そのレース内順位、
  course_predicted、tilt、exhibit_weight/exhibit_adjusted_weight（直前計量の
  実測値。v1_basic.weightは番組表発表時点の公表体重で別物のため同名衝突を
  避けてexhibit_接頭辞を付けた。当初"weight"のまま実装し、polarsの
  select時にDuplicateErrorで発覚・修正）、プロペラ/部品交換の有無(0/1)、
  気象6項目(気温・風速・風向コード・波高・水温・天候コード)の計17特徴量。
  天候コードはWEATHER_CONDITION_CODES(晴=0/曇り=1/雨=2、実データで観測
  された値のみ明示マッピング、未知の値はNoneで当て推量しない)、風向コードは
  実際の方角との対応が未確定(前述参照)なのでLightGBMのcategorical_feature
  として扱う(course_predictedと同じ理由で名義尺度扱い)。
- リーク検証は`race_before_info.source`で分岐する（詳細は上記
  「captured_at の意味とsource列の追加」参照）。現在の3ヶ月分は全件
  `source='backfill'`のため、captured_atでの機械的検証ではなく
  サイト挙動(締切T-14〜16分公開・締切後不変)を根拠にしている。
- データが存在する2026-06-21〜09-20のみで検証。本番モデルの学習・検証期間
  (2023-09-01〜2025-12-31 / 2026-01-01〜09-17)とは別に、この実験専用の
  時系列split(学習: 06-21〜08-20、9,936レース / 検証: 08-21〜09-20、
  4,836レース)を`ml/src/ml/models/exhibition_experiment.py`で使う。
  **検証期間が約1ヶ月と短く、本番の検証期間(約8.5ヶ月)に比べて代表性は
  限定的**な点に注意。
- **v1+v2+v3 と v1+v2+v3+v5 の比較（この専用split、binary、同一パラメータ）**:
  的中率 55.36%→55.54%(+0.19pt)、log loss 1.2359→1.2196(-0.0163)、
  Brier 0.6002→0.5944(-0.0058)。**v4_stadiumの時は改善なしだったが、v5は
  3指標とも一貫して改善**（motor_win_rate_2修正やハイパーパラメータ
  チューニングの改善幅よりさらに大きい）。feature importanceでは
  `course_predicted`が5位、`exhibit_time_dev_from_race_avg`が6位に入るなど
  v5特徴量が上位に複数入っており、既存特徴量と重複せず新しい情報を
  提供していると考えられる。欠損率も0.02〜0.42%と低く実用的なカバレッジ。
- confident_top3(p_top3>=96%、CLAUDE.md「p_top3の精度検証」参照)でも比較:
  該当数3,077→3,180件(+103件)、実績的中率88.30%→88.43%(+0.13pt)。
  該当数・精度の両方が同時に改善しており、量と質のトレードオフではない。
- 本番モデルへの組み込みはまだ行っていない（ユーザー判断待ち）。財団への
  利用許諾確認が未解決のままbeforeinfoのバックフィルを続行し特徴量化した
  経緯を踏まえ、本番投入の可否は別途判断が必要。

### 全期間バックフィル完了とv5の本番split検証（2026-10-03）
- beforeinfoのバックフィルを1年単位で4回に分けて実施し、DB上の全レース
  (2023-09-01〜2026-10-03、172,896レース)を完全にカバーした。各回とも
  パースバグは0件（3ヶ月分の時に見つかった`&nbsp;`ケース修正で解消済み）、
  失敗は全て一時的なDNS解決失敗で`--retry-failed`により最終的に全件成功。
  `race_before_info` 1,037,376行(172,896×6)・`race_weather_info` 172,896行、
  races総数と完全一致を確認済み。
- v5_exhibitionを全期間で再生成し、**本番と同じ時系列split**
  (学習2023-09-01〜2025-12-31・129,684レース / 検証2026-01-01〜09-17・
  40,692レース)で比較。短期間split(1ヶ月検証)の時より改善幅が明確に拡大:

  | | 的中率 | log loss | Brier |
  |---|---|---|---|
  | A) v1+v2+v3 | 56.20% | 1.1974 | 0.5834 |
  | B) v1+v2+v3+v5 | 56.69% | 1.1799 | 0.5751 |
  | 差分 | +0.49pt | **-0.0175** | **-0.0083** |

  motor_win_rate_2修正・ハイパーパラメータチューニング・v4_stadiumいずれの
  改善幅も上回る、これまでで最大の改善。feature importanceでは
  **`course_predicted`が全特徴量中1位**(gain=556,051.6、2位の
  `lane_win_rate_recent50_rank`の約3倍)となり、支配的な特徴量になっている。
  `exhibit_time_dev_from_race_avg`も6位。欠損率は0.6〜1.3%と低い。
- **confident_top3(p_top3>=96%)の比較（2026-10-03、手法を本番APIに合わせて修正後の値）**:

  | | 該当数 | 実績3着以内率 |
  |---|---|---|
  | A) v1+v2+v3 | 22,428 | 90.37% |
  | B) v1+v2+v3+v5 | 23,115 | 90.50% |
  | 差分 | +687 | +0.13pt |

  初回実測時はA=89.38%(22,914件)となり、2026-09-21出荷時の実績
  (90.37%、22,429件)と食い違っていたため原因を調査した。
  `ml/src/ml/models/exhibition_experiment.py`の`_confident_top3_report()`に
  本番API(`PerformanceController::confidentTop3Overall()`)との
  **2点のメソッド差異**があったことが原因と判明（モデルの差・判定データの
  更新ではない）:
  1. 本番は「レースごとにp_top3が最大の1艇だけ」を候補にし、その1艇が
     閾値以上かを見る(`rank() OVER (...) WHERE rnk=1`)。初回実装は
     レース内の順位を無視し、p_top3>=閾値を満たす艇を全艇カウントして
     いたため、同一レースで複数艇が閾値を超えるケースを余分に数えて
     母数・的中率の両方がズレていた。
  2. 本番は`race_results`にINNER JOINしており、結果行が存在しない艇は
     分母からも除外される。初回実装はLEFT JOINの結果(finish_pos NULL)を
     「失格でNULL」も「行自体が無い」も同じ0扱いにしており、後者を
     誤って分母に含めていた。
  この2点を修正し(`fetch_all_dataset`/`fetch_v5_dataset`に
  `has_result_row`列を追加して判別可能にした)、A)を本番splitで
  再実測したところ22,428件・90.37%となり、2026-09-21の実績
  (22,429件・90.37%)と1件差（丸め誤差の範囲）で一致した。
  **結論: 本番側(`PerformanceController`)の実装は正しく、修正が必要
  だったのはこのml/実験スクリプト側のみ**。v5導入によるconfident_top3への
  真の効果は、的中率+0.13pt・該当数+687件で、A/Bとも既に90%を超えている
  （「Bで初めて90%を超えた」という初回の解釈は誤りだったので撤回する）。
- 本番モデルへの組み込みはまだ行っていない（ユーザー判断待ち）。

### v5モデルの推論時欠損耐性の検証（2026-10-03）— ライブ取得停止中は本番投入不可
- ライブ取得(beforeinfo)が財団許諾待ちで停止中の間にv5モデルを本番投入した
  場合を想定し、本番splitで3パターンを比較した
  (`ml/src/ml/models/exhibition_experiment.py`):
  - A) v1+v2+v3（v5なしで学習・推論）
  - B) v1+v2+v3+v5（v5あり、通常の学習・推論）
  - C) v1+v2+v3+v5**で学習**した同じboosterに対し、**推論時だけv5の
    全列をNaNにして**予測（ライブ取得停止中に新規レースを推論する状況を模す）

  | | 的中率 | log loss | Brier | confident_top3該当数 | confident_top3実績 |
  |---|---|---|---|---|---|
  | A) v5なし | 56.20% | 1.1974 | 0.5834 | 22,428 | 90.37% |
  | B) v5あり(通常) | 56.69% | 1.1799 | 0.5751 | 23,115 | 90.50% |
  | C) v5あり学習→推論時v5=NaN | **31.07%** | **1.6544** | **0.7820** | **133** | **47.37%** |

  **Cは全指標でAより大幅に悪化し、特にconfident_top3は実質機能しなくなる**
  （該当数が22,428→133に激減し、残った133件の実績的中率も47.37%で
  「90%保証」が完全に崩壊する）。LightGBMは欠損値をネイティブに扱えるが、
  学習時にv5の欠損率が0.6〜1.3%程度しかなかったため、モデルが
  `course_predicted`(重要度1位、gain=556,051.6、2位の約3倍)を筆頭に
  v5特徴量へ強く依存する分岐を多数学習しており、推論時に欠損率が
  100%になると学習時の欠損パターンの想定から大きく外れ、分岐の既定方向
  (欠損時のデフォルト経路)が系統的に誤った方へ送られるため。
  「v5特徴量が使えなければv1+v2+v3相当の性能に自然劣化する」という
  楽観的な想定は誤りで、**実際には素のv1+v2+v3モデルより明確に悪化する**。
- **結論：ライブ取得(beforeinfo)が停止中の間は、v5を含むモデルを本番に
  投入してはならない**。v5モデルを投入する場合は、将来レースに対して
  常にv5特徴量が実際に取得できている状態（ライブ取得が財団許諾を得て
  再稼働していること）が前提条件になる。もしライブ取得が将来止まった
  場合に備えるなら、v5欠損時にv1+v2+v3モデルへ自動フォールバックする
  仕組みが必要（現状未実装）。

## p_top3の直接学習モデル（2026-10-03）
- `ml/src/ml/models/top3.py`。目的変数をis_winner(1着)ではなく
  is_top3(finish_posが1,2,3のいずれか)に変えて直接二値分類で学習し、
  現行手法(is_winnerモデル→Plackett-Luce展開でp_top3を導出)と比較した。
  特徴量(v1+v2+v3)・split(本番と同じ)・パラメータは完全に揃え、
  目的変数だけを変えている。正規化はAが合計1(1着予測の性質)、Bは
  合計3(3着以内は必ず3艇)。Bの素のシグモイド出力は艇間に制約が無いため、
  正規化後に値が[0,1]を超えることがある(244,152件中1,323件=0.54%で発生、
  log loss/Brier計算時は[eps,1-eps]にクリップして計算)。
- **艇単位の3着以内的中率**(上位3艇選択): A) 68.42%(83,521/122,076) →
  B) 68.84%(84,041/122,076)。+0.42ptの僅かな改善。
- **log loss/Brier**(is_top3を2値ラベルとした艇単位評価): A) 0.6034/0.2020
  → B) 0.5763/0.1934。Bが両方で改善。
- **confident_top3(本番API定義、閾値0.96)**: A) 22,428件・90.37% →
  B) 3,909件・**94.60%**。Bは該当数が大きく減る代わりに、該当した場合の
  精度はAより明確に高い。
- **閾値スイープ**(0.90/0.92/0.94/0.96/0.98、本番API定義):

  | threshold | A:該当数 | A:実績 | B:該当数 | B:実績 |
  |---|---|---|---|---|
  | 0.90 | 33,179 | 86.69% | 12,024 | 92.41% |
  | 0.92 | 30,670 | 87.54% | 8,817 | 93.16% |
  | 0.94 | 27,365 | 88.82% | 6,080 | 93.78% |
  | 0.96 | 22,428 | 90.37% | 3,909 | 94.60% |
  | 0.98 | 13,212 | 92.46% | 2,351 | 95.36% |

  **Bはスイープした最低閾値(0.90)でも既に92.41%で90%を超えている**ため、
  今回の範囲(0.90〜0.98)だけでは「実績90%を超える最小の閾値」は特定できて
  いない。実際の交差点はテストした範囲の外(0.90未満)にある可能性が高く、
  より広い範囲（例: 0.70〜0.90を0.02刻み等）で追加のスイープが必要。
  AとBの確率スケールは直接比較できない点に注意（Bの0.90はAの0.96超えより
  実績精度が高い＝Bの方が同じ数値でも「保証」として強い）。
- **feature importance**: 1着予測(A)と3着以内予測(B)で寄与する特徴量が
  明確に異なる。Aは`lane`(枠番)が1位(gain=496,651.8、2位の2倍以上)で
  構造的な「1号艇優位」が1着予測を支配するが、Bでは`lane`は3位
  (gain=103,815.7、Aの約1/5)に後退し、`lane_win_rate_recent50_rank`
  (選手個人の枠番別成績の順位)・`national_win_rate_dev`が1・2位を占める。
  また`avg_finish_pos_recent10`はAで20位だがBでは7位に上昇しており、
  直近の平均着順という「着順そのもの」に近い特徴量は、1着という
  狭い的中よりも3着以内という緩い基準の予測に効きやすいと考えられる。
- 本番モデルへの組み込みはまだ行っていない（ユーザー判断待ち）。v5と同様、
  推論時の欠損耐性やconfident_top3の運用方針（Bを使うなら閾値の再設計が
  必要）は別途検討が必要。

### Bの閾値スイープを下方向(0.70〜0.90)に拡張（2026-10-03）
- `uv run python -m ml.models.top3 --low-sweep`で0.70〜0.90を0.02刻みで
  追加検証した結果:

  | threshold | 該当数 | 実績 | 1日あたり |
  |---|---|---|---|
  | 0.90 | 12,024 | 92.41% | 46.25 |
  | 0.88 | 15,481 | 91.66% | 59.54 |
  | 0.86 | 18,902 | 91.10% | 72.70 |
  | **0.84** | **22,303** | **90.36%** | **85.78** |
  | 0.82 | 25,315 | 89.65% | 97.37 |
  | 0.80 | 28,194 | 88.91% | 108.44 |
  | 0.78〜0.70 | 30,725〜37,745 | 88.19%〜85.71% | 118.17〜145.17 |

  - **実績が90%を下回る境界: 閾値0.84(90.36%、まだ90%以上)と0.82(89.65%、
    90%未満)の間**。
  - **90%を保ったまま該当数を最大化する閾値: 0.84**（該当数22,303、
    実績90.36%、1日あたり85.78艇）。
  - **現行A(22,428件・90.37%・1日約86艇)とほぼ同じ水準**で、
    B@0.84は該当数22,303件・1日85.78艇とわずかに少ない（-125件、
    -0.48艇/日）。**「Bに切り替えれば同じ90%保証でAより多くの艇数が
    取れる」という期待は支持されなかった**。B=0.84とA=0.96という
    全く異なる数値で同じ約90%・同じ約86艇/日という結果に収束する点が
    示唆的で、2つのモデルの確率スケールは違えど「90%保証で選べる艇の
    実質的な上限」は現行Aのアプローチでほぼ天井に達している可能性がある。

### Bの本番組み込み検討: 該当数を揃えた厳密比較とキャリブレーション（2026-10-04）
- `uv run python -m ml.models.top3 --deep-dive`で2点を検証した。

**1. 該当数を揃えた厳密比較**（同じ件数になる閾値同士で実績を比較）:

  | 揃えた件数 | A実績 | B実績 | 差分(B-A) |
  |---|---|---|---|
  | 22,428件(A=0.96相当) | 90.37% | 90.32%(閾値0.8393) | -0.05pt |
  | 13,212件(A=0.98相当) | 92.46% | 92.20%(閾値0.8935) | -0.26pt |
  | 30,670件(A=0.92相当) | 87.54% | 88.21%(閾値0.7805) | +0.66pt |
  | 33,179件(A=0.90相当) | 86.69% | 87.41%(閾値0.7579) | +0.72pt |

  **confident_top3が実際に使う90%保証ライン付近(22,428件/13,212件)では
  件数を揃えるとAとBの差はほぼ無い（-0.05pt/-0.26pt、Bがむしろ僅かに
  劣る場合もある）**。Bの優位は件数が多い・閾値が低い領域(30,670件/
  33,179件)でのみ+0.66〜+0.72ptと明確に出る。confident_top3の運用範囲
  だけを見るなら「件数を揃えれば差がない」という事前の懸念は**部分的に
  正しい**（90%保証ラインでは差がない。ただしもっと緩い基準では差が出る）。

**2. キャリブレーション（10分位、has_result_rowが真の全艇対象）**:

  | | A最大のズレ | B最大のズレ |
  |---|---|---|
  | 低確率帯(5%付近) | **+9.60pt**(bin2) | -0.83pt(bin1) |
  | 高確率帯(83〜96%帯) | **-13.23pt**(bin9) | +1.14pt(bin9) |
  | 全10bin | -13.23pt〜+9.60pt | -1.13pt〜+1.14pt |

  Aの値は「p_top3の精度検証（2026-09-21）」で記録済みの値
  (+9.6pt/-13.2pt)とほぼ完全に再現し、既存の知見と整合する。
  **Bは全binで±1.14pt以内に収まっており、Aの最大13pt超のズレと比べて
  桁違いに正直な確率を出している**。「低確率帯で弱気・高確率帯で強気」
  というAの系統的なバイアスが、Bでは実質的に解消されている。

- **結論（今回の2点を踏まえた判断材料）**: confident_top3の90%保証ライン
  単体で見ればA→Bの切替メリットは乏しい（件数も精度もほぼ同等）。
  一方、**確率の精度（キャリブレーション）は圧倒的にBが優れており**、
  「的中率と回収率は必ず並記する」等、確率の意味そのものを製品的に使う
  場面（グレード分けの閾値設計、別の確率帯での新機能、ユーザーへの
  確率表示等）では、Aの±13pt級のバイアスを前提にした補正なしにそのまま
  使うのは危険。Bの方がそうした用途に素直に使える。
  判断はユーザー側で行う（本番組み込みはまだ行っていない）。
- 残課題: Bの推論時v5欠損耐性は未検証（v5実験と同様の問題が起きないか
  要確認。ただしBはv1+v2+v3のみで学習しているため今回は無関係）。
  B正規化後の[0,1]超え(0.54%)をどう扱うか（単純clip等)も実装時に決める
  必要がある。

## 本番モデルを2本立て構成に変更（2026-10-04）
- 上記の検証を踏まえ、Bを「3着以内予測専用モデル」として本番に組み込んだ。
  1着予測モデル(is_winner)はそのまま残し、confident_top3(軸艇)だけを
  3着以内予測モデル(is_top3)に切り替える**2モデル構成**にした。

### モデル学習・保存（`ml/src/ml/models/train.py`）
- `MODEL_KINDS`で`winner`(is_winner, prefix=v3_binary)と`top3`(is_top3,
  prefix=v3_top3)を定義。デフォルト`--kind both`で1回のコマンドで両方を
  学習・保存する（特徴量・学習期間・パラメータは完全に同じ、目的変数と
  学習関数(`lgbm.train_model` / `top3.train_top3_model`)だけが違う）。
  `uv run python -m ml.models.train`で
  `v3_binary_20261004.pkl`/`v3_top3_20261004.pkl`を保存済み。

### 推論（`ml/src/ml/models/predict.py`）
- CLI引数を`predict.py <winner_model_version> <top3_model_version> <date>`
  に変更。レースごとに**predictionsを2行**書く:
  - winner_model_versionの行: p_firstのみ。p_top2/p_top3はNULL（既存のまま）。
  - top3_model_versionの行: p_top3のみ。p_first/p_top2はNULL。
  - p_top2は恒久的にNULL（Plackett-Luce由来の値を残すと「どちらのモデルの
    定義か」が混在するため廃止）。
  - 既存(race_id, model_version, stage)の存在チェックはモデルごとに独立に
    行うため、片方だけ欠けている状態からの再実行でも安全に埋められる。
  - top3モデルの正規化後[0,1]超え値は`ml.models.top3.predict_race_sum3_for_inference`
    で単純に`clip(0,1)`し、clip件数を標準出力に出す
    (`ml/src/ml/models/top3.py`に実装、研究用の`predict_race_sum3`とは別関数。
    検証期間一括生成では244,152件中1,318件=0.54%でclipが発生、事前の
    研究結果(1,323件)とほぼ一致)。
- `judge.py`に実装漏れのバグ対策を追加: p_firstが全NULLのtop3専用行に対して
  「ORDER BY p_first DESC LIMIT 1」を素朴に実行すると、NULL同士の順序は
  不定で任意のlaneを「的中/不的中」と誤判定してしまうため、p_firstが
  1件でも入っている行だけを対象にするEXISTS条件を追加した。

### PHP側
- `config/ml.php`: `prediction_top3_model_version`(env
  `PREDICTION_TOP3_MODEL_VERSION`)を新設。`top3_confident_threshold`を
  **0.96→0.84**に変更（根拠: 新モデルの検証期間実データで、本番API定義
  (レースごとにp_top3最大の1艇)のまま0.70〜0.90を0.02刻みでスイープし、
  実績90%を保ったまま該当数を最大化する境界が0.84(22,303件・90.36%)
  だったため。0.82では89.65%で90%を下回る）。
  `top3_confident_threshold_options`を`[0.96,0.97,0.98,0.99]`→
  `[0.80,0.84,0.88,0.92,0.96]`に変更（新モデルの確率スケールに合わせた）。
- `app/Models/Race.php`: `top3Prediction()`(HasOne、
  `prediction_top3_model_version`に絞る)を追加。既存の`prediction()`は
  1着予測モデルのまま変更なし。
- `app/Http/Controllers/Api/RaceController.php`:
  `top3Prediction.entries`を`today()`/`show()`両方でeager load。
- `app/Http/Resources/RaceSummaryResource.php` /
  `RaceDetailResource.php`: p_top3は`$this->top3Prediction`から取得する
  よう変更（p_firstは従来通り`$this->prediction`から）。
- `app/Http/Controllers/Api/PerformanceController.php::confidentTop3()`:
  使う`model_version`を`prediction_model_version`→
  `prediction_top3_model_version`に変更（SQL自体は無修正、パラメータの
  差し替えのみ）。
- `app/Console/Commands/PredictionsGenerateToday.php`: `--top3-model-version`
  オプションを追加し、`ml.models.predict`に2つのmodel_versionを渡すよう
  変更。`tickets:generate-today`はp_firstしか使わないため無変更
  （1着予測モデルのmodel_versionのみ渡す）。
- `.env`/`.env.example`: `PREDICTION_MODEL_VERSION`を
  `v3_binary_20260920`→`v3_binary_20261004`に、
  `PREDICTION_TOP3_MODEL_VERSION=v3_top3_20261004`を新設。

### フロントエンド
- `resources/js/pages/RacesToday.vue`: 「3着以内に入る確率が96%以上の艇。
  検証期間で実績90.4%（22,429艇中20,270艇的中）」→「84%以上...
  （22,303艇中20,154艇的中）」に変更。
- `resources/js/pages/Performance.vue`: 閾値セレクタのデフォルト・
  フォールバック候補を`0.96`/`[0.96,0.97,0.98,0.99]`→`0.84`/
  `[0.80,0.84,0.88,0.92,0.96]`に変更。

### 検証・実行結果
- 検証期間(2026-01-01〜09-17、40,692レース)を新モデルで一括生成→
  `predictions:judge`相当(`ml.models.judge`)を実行。winner/top3とも
  races=40,500(+既存テスト分192=40,692)で欠落なし。judge結果:
  judged=40,023 hit=22,755 hit_rate=56.85%（研究時の推定56.20〜56.69%と
  整合）。本番API実測: confident_top3 該当22,309件・的中20,160件・
  実績90.37%（研究時の推定22,303件・90.36%とほぼ一致、6件の差は
  clip処理の違いによるものと考えられる）。
- 旧モデル(v3_binary_20260919/20260920)のpredictions/tickets/judgmentsは
  model_version違いでそのまま残置（上書きなし）。
- 当日分(2026-10-04)も`races:fetch-today`→`predictions:generate-today`で
  新モデルから作り直し済み（156レース、winner/top3とも936 entries、
  tickets 1,232件生成）。
- 残課題だった`DataCoverage`のtop3欠損検知漏れは2026-10-04に対応済み
  （下記「DataCoverageのtop3モデル欠損検知」参照）。

## DataCoverageのtop3モデル欠損検知（2026-10-04）
- `has_predictions`がwinner model_versionの存在しか見ておらず、
  top3モデル側だけ欠けていても「揃っている」と誤判定する問題を修正した。
- `data_coverage`に`has_top3_predictions`列を追加
  （migration: `2026_10_04_100001_add_has_top3_predictions_to_data_coverage_table.php`）。
  1フラグに統合せず**別カラムに分離**した（results/payoutsと同じ方針。
  どちらが欠けているか区別できる方が運用上有用なため）。
  `DataCoverage::refreshCoverage()`は`$modelVersion`(winner)と
  `$top3ModelVersion`(top3)を両方受け取り、それぞれ独立に
  `races.n_races`との一致を見て`has_predictions`/`has_top3_predictions`を
  算出する。
- `missingFieldsFor()`/`criticalGapsFor()`に`top3_predictions`を追加
  （`criticalGapsFor`の当日判定にも`top3_predictions`を含めた。
  `data:catch-up:status`は`criticalGapsFor()`を汎用的に使っているため
  無修正で自動的に反映される）。
- `DataCatchUp.php`: `config('ml.prediction_top3_model_version')`を
  追加で読み、`refreshCoverage()`の全呼び出しに渡すよう変更。
  `fillDate()`の再生成トリガーを「predictionsまたはtop3_predictionsの
  どちらかが欠けていれば`predictions:generate-today`を実行」に変更
  （`predictions:generate-today`は(race_id, model_version, stage)単位で
  既存分をスキップするため、片方だけ欠けている状態からの再実行でも
  無駄なく埋まる）。日次・月次のレポート文言にも`top3_predictions`を反映。
- **直近30日の突き合わせ結果（2026-09-05〜10-04）**: winner/top3の
  predictions件数を日付ごとに直接比較したところ、**両モデルの件数は
  全日程で完全に一致**し、片方だけ欠けている検知漏れは見つからなかった
  （どの日も「両方0件」または「両方races件数と一致」のいずれか）。
  一方、2026-09-18〜10-03の16日間はwinner/top3とも0件（完全な未生成）
  だった。これは今回の2モデル化固有の問題ではなく、このセッションでは
  `data:catch-up`相当の日次バッチが実際のcron/systemdスケジュールで
  継続実行されておらず、対話的に生成した日付（検証期間の2026-01-01〜
  09-17と当日2026-10-04）以外が単純に未処理だったため。本番でcron/systemd
  が正常稼働していれば発生しない種類のギャップであり、`has_top3_predictions`
  追加前から存在した一般的な運用ギャップ（新しい検知漏れではない）。
  この16日分のpredictions生成は今回のタスク範囲外のため実施していない
  （ユーザー判断待ち）。

## 中止レースを考慮したhas_results/has_payoutsの修正（2026-10-06）
- `has_results`/`has_payouts`は「その日の全レース数と一致して初めてtrue」と
  定義していたため、荒天等で一部レースが中止になった日は、races行自体は
  B(番組表)時点で作られているのに結果が永久に来ないため、`data:catch-up`が
  いつまでも「欠損あり」と報告し続ける問題があった（2026-09-21: 戸田・
  江戸川が全12R中止、津5〜12R中止、三国10〜12R中止。2026-09-22: 津が全12R
  中止。Kファイルの[払戻金]概況表に「5R　中止」という形で明記されている）。
- 対応案は2つ検討した: 1) `races`に`cancelled`フラグを追加しKファイルの
  「中止」表記から機械的に判定、2) 一定期間(例:3日)結果が来ないレースを
  推定で「中止扱い」にする。**1を採用**（2が正確性に欠けるため。単なる
  配信遅延と中止の区別がKファイル自体に明記されているのに、タイムアウトで
  推測するのは不要かつ不正確）。
- 実装:
  - `races`に`cancelled`(boolean, default false)列を追加
    （migration: `2026_10_06_100001_add_cancelled_to_races_table.php`）。
  - `ml/src/ml/parsers/result.py`: 中止レースは着順ブロック(NR形式の見出し+
    着順6行)自体が存在せず、場ヘッダ内の[払戻金]概況表（全レースを一覧
    表示する自由形式の表。開催日の行から最初の実レース見出しの直前までに
    必ず1回だけ現れる）に「5R　中止」の形でのみ記録される。この区間を
    スキャンする既存ループ（元々は「全レース中止で0件のまま次の場へ」の
    判定にのみ使っていた）に正規表現`_CANCELLED_RACE_RE`での検出を追加し、
    `ParsedResult.cancelled`（stadium_code/race_date/race_noのリスト）として
    返すよう変更。全面中止・一部中止のどちらも同じループ1箇所の変更で
    両対応できた（概況表は中止/開催済みを問わず全レース分が同じ1箇所に
    まとまって出るため）。
  - `ml/src/ml/loaders/results.py`に`mark_cancelled_races()`を追加。
    対応する`races`行が無ければ`LoaderError`を送出する（race_results/
    payoutsと同じ「黙って捨てない」方針）。一度中止と確定したレースが
    後から取り消されることはない（Kファイルは確定後の最終結果）ため、
    falseへの書き戻しは行わない。`ml/src/ml/loaders/cli.py`の
    `load-results`、`ml/src/ml/loaders/backfill.py`の全期間バックフィル
    両方の経路に組み込み済み。
  - `app/Models/DataCoverage::refreshCoverage()`: `race_counts`に
    `n_completable_races`(cancelled=falseの件数)を追加し、has_results/
    has_payoutsの分母をこちらに変更。has_predictions/has_top3_predictions
    は従来通り`n_races`(全件)のまま据え置き（中止は結果確定後にしか
    判明せず、予測自体は締切前に正常に生成されているはずなので分母から
    除外する理由がない）。
- 検証: `ml/tests/test_result_parser.py`にbackfill時キャッシュ済みの実
  ファイル(K260921.TXT=全面+一部中止混在、K260922.TXT=単一場全面中止)を
  使った回帰テストを追加（中止レースの集合が一致すること、中止レースが
  results/payoutsのどちらにも現れないこと等、4件）。ml側テスト全59件
  パス。`data:catch-up --days=20`を再実行し、2026-09-21(cancelled=35件)・
  2026-09-22(cancelled=12件、どちらも手動で数えた件数と一致)で
  has_results/has_payoutsが実際にtrueへ反転したことを確認済み。
- 2026-09-19は別種の理由で`has_payouts=false`のまま残っていた（下記
  「2026-09-19の不成立レースとhas_payoutsの修正」で解消済み）。

## 2026-09-19の不成立レースとhas_payoutsの修正（2026-10-06）
- 上記の中止レース対応とは別に、2026-09-19は戸田9Rが「不成立」
  （複数フライング等でレース自体が成立しなかった扱い。`_VALID_STATUSES`の
  "00"相当）で、3連単・3連複は不成立で払戻自体が存在しない一方、2連単のみ
  「1-2  100円」という払戻が実在するという混在ケースがあり、
  `has_payouts=false`のまま残っていた。中止(races.cancelled)とは別の現象
  （レース自体は行われている。race_resultsは6艇分とも正常に存在する）。
- `data:catch-up:status`（前日・当日のみをstrictに見るヘルスチェック）は
  2026-09-19のような過去日を通常は見ないため直接の影響はなかった
  （実際に`php artisan data:catch-up:status`を実行し`OK`を確認済み）。
  ただし同種の「一部式別のみ不成立」が将来別の日に起きた場合、その日が
  「前日」に該当するタイミングで一時的にNGを出し続けることになり、
  監視の信頼性を損なうため、恒久的な対処を行った。
- 対応案は2つ検討した: 1) `payout_counts`の判定を「3連単の払戻が存在する」
  から「いずれかの式別の払戻が存在する」に変える、2) 「不成立」をracesに
  記録する別フラグを追加（中止フラグと同様の構成）。**発生頻度が低い
  （観測1件のみ）ため1を採用**。`has_payouts`の実際の用途は「払戻データの
  投入自体ができたか」の監視であり（`payout_counts`自体を式別ごとの
  回収率計算等に使っている箇所は無いことをgrepで確認済み）、式別を問わず
  1件でも払戻が存在すればデータ投入は成功しているとみなしてよいため、
  2のような専用フラグ・パーサ改修は過剰と判断した。
- `app/Models/DataCoverage::refreshCoverage()`の`payout_counts`CTEから
  `AND po.bet_type = '3連単'`条件を削除し、レースに対して任意の式別の
  payout行が1件でも存在すれば`has_payouts`の分子としてカウントするよう
  変更。マイグレーション・パーサ変更は不要（SQLの条件変更のみ）。
- 検証: 2026-09-19を`refreshCoverage()`で再計算し、`has_payouts`が
  false→trueに反転したことを確認。2026-09-15〜10-05の全日程で
  races/results/payouts/predictions/top3_predictionsが揃っていることを
  再確認済み（10-06当日のみresults/payouts=false、結果未確定のため正常）。
  `data:catch-up:status`は引き続き`OK`。

## stage2構成（直前再予測、v5_exhibitionを含む）の導入（2026-10-08）
- v5モデル(v5_binary_20261008/v5_top3_20261008)の学習・保存・再現確認が
  完了したことを受け、本番に2段階予測構成（stage1=v3、stage2=v5）を導入した。
  以下は設計・判断の根拠の記録。実データでの稼働結果（明日以降の初回稼働分）は
  別途追記する。

### 1. なぜ2段構成が必要だったか
- beforeinfo(直前情報)は各レース締切のT-14〜16分に公開される
  （CLAUDE.md「直前情報(beforeinfo)の取得」参照）。一方、本番の予測バッチ
  (`predictions:generate-today`)は06:10に当日分をまとめて実行するため、
  06:10時点ではその日の大半のレースの締切はまだ何時間も先であり、
  v5_exhibition特徴量は存在しない。
- 「v5モデルの推論時欠損耐性の検証」（2026-10-03）で実測済みの通り、
  v5を含むモデルに対して推論時だけv5特徴量を全NaNにすると、的中率が
  56.69%→31.07%、confident_top3が23,115件・90.50%→133件・47.37%まで
  崩壊する。06:10の一括バッチでv5モデルをそのまま使うと、この最悪
  ケースがほぼ毎日そのまま現実化する構成になってしまう。
- `predictions.stage`は最初からこの用途を想定して設計されていた
  （`database/migrations/2026_09_19_100004_create_predictions_tables.php`
  のコメント「stage=1/2は将来の『締切前の早い段階の予測』『締切直前の
  最終予測』等の2段階publishを想定した区分」、`CHECK (stage IN (1, 2))`、
  `unique(race_id, model_version, stage)`）。stage2はこの既存設計を
  初めて実際に使う形になった。

### 2. ライブ取得の再開判断
- 財団(一般財団法人BOATRACE振興会)への利用許諾確認は2026-09-21に
  問い合わせて以降、回答が無いまま2026-10-08に至った。回答を待たず
  ユーザー判断で`beforeinfo:schedule-today`のスケジュール登録を再開した
  （backfill実行時と同じ、ユーザーがリスクを受容する形の判断）。
- 負荷の見積り: ライブ取得は1レースにつき1リクエスト、1日最大288
  リクエスト（0.003 req/s相当）。既に実施した全期間backfill（0.39 req/s
  を約120時間）と比べて2桁軽い負荷であり、サイト運営への影響という
  観点では既存の実績の範囲内に収まる。

### 3. 捕捉タイミングT-12分の根拠（T-15分への変更は却下）
- 前回の調査時点でいったんT-15分への変更を検討したが、却下した。
  beforeinfoの公開はT-14〜16分の幅があり、T-15分で捕捉しようとすると
  公開のタイミングによっては「データがありません」として失敗する確率が
  T-12分より上がる（公開前に当たりうる）。
- T-12分なら、最も遅い公開(T-14分)でも既に2分前に公開済みであることが
  保証され、かつcutoff_at(=締切10分前)まで2分の余裕を持ってリーク検証
  (`captured_at <= cutoff_at`)を満たせる。既存の`ScheduleBeforeInfoCapture`
  の実装（`subMinutes(12)`固定）はそのまま維持した。

### 4. 毎分バッチの設計（事前チェック・--race-ids・withoutOverlapping）
- `predictions:generate-stage2`は当初、対象レースの有無に関わらず無条件で
  `uv run`を3回（`ml.features.exhibition`/`ml.models.predict`/
  `ml.models.tickets`）起動していた。実測したところ、該当レースが0件でも
  実時間で約0.8秒・CPU時間で約5.3秒を消費する（主にLightGBMモデルの
  ロード）。毎分×1日1440回この無駄打ちが積み重なるため、対象レースの
  事前チェックを追加した。
  - `PredictionsGenerateStage2::findEligibleRaceIds()`で「live
    beforeinfoが揃っており(`race_before_info.source='live'`の件数が
    `race_entries`件数と一致)・cutoff_at前(`deadline_at - 10分 > now()`)・
    stage2未生成(winner/top3のどちらかが未生成)」を1クエリで判定し、
    0件ならuv runを一切呼ばず即終了する。実測で0.8秒→0.094秒に短縮。
  - `ml.features.exhibition`に`--race-ids`を追加した。以前は「今日1日分」
    を毎回まるごとupsertする設計だったため、ライブ捕捉が進むにつれて
    対象行が増え続け、夕方には捕捉済み全レース分（最大288レース×6艇=
    1,728行/分）を毎分upsertし続けることになる計算だった。事前チェックで
    特定したrace_idリストだけを渡すことで、この増加を防いだ。日付範囲
    指定のみの既存呼び出し（バックフィル・過去分の一括生成）は無変更。
  - `routes/console.php`の`predictions:generate-stage2`に
    `withoutOverlapping(10)`を追加した。1回の処理が60秒を超えて次回の
    起動と重なると、同じレースを並行処理してpredictions存在チェックが
    競合する恐れがあるため。expiresAt=10分はプロセス異常終了時にロックが
    残り続けないための安全弁（通常の処理は事前チェックのおかげで数秒〜
    瞬時に終わる想定）。cache driverは`database`（`cache_locks`テーブル
    による原子的ロックに対応していることを確認済み）。
  - `ml.models.predict`自体（推論本体）はrace_id絞り込みをしていない
    （「今日1日分、cutoff_at前のみ」のまま）。固定コストがLightGBMモデルの
    ロードであり走査行数にほぼ依存しないため、ここを絞る効果は薄いと
    判断した。

### 5. stage優先ルールと二重計上の罠
- 「レースごとにstage2の予測があればそれ、無ければstage1」という優先
  ルールを、`Race::effectivePrediction()`/`effectiveTop3Prediction()`を
  唯一の参照経路として全API・全集計で統一した（`RaceSummaryResource`/
  `RaceDetailResource`/`PerformanceController`）。
- **罠**: `predictions:judge`はstage1/stage2を独立に判定するため、
  stage2の予測が存在するレースは`prediction_judgments`に2行
  （stage1分のprediction_idに対する行とstage2分のprediction_idに対する
  行）入る。`PerformanceController`の集計SQLで素朴に
  `WHERE p.model_version IN (stage1版, stage2版) AND p.stage IN (1, 2)`
  と絞ると、この2行が両方ヒットして同一レースが二重計上される
  （的中数・レース数・買い目点数・回収額の全てが水増しされる）。
  - 対策として、必ず`SELECT DISTINCT ON (race_id) id, race_id FROM
    predictions WHERE model_version IN (?, ?) AND stage IN (1, 2)
    ORDER BY race_id, stage DESC`という形のCTE（`active_winner`/
    `active_top3`）を経由し、レースごとに1つのprediction_idへ絞ってから
    `prediction_judgments`/`prediction_tickets`をJOINすること。
    `model_version`に渡す2値のうちstage2側がnull（未設定環境）でも、
    `model_version = NULL`は何にも一致しないため安全にstage1のみの
    挙動にフォールバックする。
  - **この罠は、将来この集計SQLに手を加える人が最も踏みやすいポイント
    なので、新しい集計クエリを追加する際は必ず`active_winner`/
    `active_top3`と同じCTEパターンを経由すること。**

### 6. 閾値0.84を据え置いた根拠
- v5_top3の閾値スイープ（0.80〜0.88を0.01刻み、本番API定義）:

  | threshold | 該当数 | 実績 | マージン(実績-90%) | 1日あたり |
  |---|---|---|---|---|
  | 0.88 | 16,022 | 92.24% | +2.24pt | 61.62 |
  | 0.87 | 17,747 | 91.90% | +1.90pt | 68.26 |
  | 0.86 | 19,400 | 91.51% | +1.51pt | 74.62 |
  | 0.85 | 21,046 | 91.20% | +1.20pt | 80.95 |
  | **0.84** | **22,646** | **90.78%** | **+0.78pt** | **87.10** |
  | 0.83 | 24,197 | 90.31% | +0.31pt | 93.07 |
  | 0.82 | 25,751 | 90.04% | +0.04pt | 99.04 |
  | 0.81 | 27,285 | 89.64% | -0.36pt | 104.94 |
  | 0.80 | 28,685 | 89.30% | -0.70pt | 110.33 |

  0.83(24,197件・90.31%)の方が該当数は多いが、90%までのマージンが
  0.31ptしかなく、月次変動で容易に割り込みうる。0.84のv3側マージン
  (0.36pt)より薄くなるため採用しなかった。
- **0.84を据え置く根拠**: v3=90.36%(22,303件)・v5=90.78%(22,646件)と
  どちらが使われても90%保証が成立し、「84%以上、実績90.4%」という
  画面文言（`RacesToday.vue`）の変更も不要になる。
- **キャリブレーション**: v3top3の最大ズレは±1.14pt、v5top3は±1.48pt
  （bin9: 予測78.13%に対し実績79.61%）で、**v5がわずかに悪化している**。
  この点は良い面だけでなく正直に記録しておく。「v5_top3の本番組み込み
  検討」（2026-10-04）で比較した「該当数を揃えた厳密比較」の結論
  （90%保証ラインでは差がほぼ無い）と整合する結果であり、v5採用の
  決め手は的中率・該当数のわずかな改善であって、キャリブレーションの
  改善ではない。

### 7. 運用上の既知の制約
- PC電源off運用のため、stage2のカバレッジは恒久的に部分的になる
  （稼働中の時間帯に締切が来たレースしかstage2化されない）。これは
  不具合ではない。そのため`data_coverage`では`has_predictions`等と同じ
  booleanフラグにはせず、`stage2_prediction_race_count`という整数
  カウント（`odds_race_count`と同じ扱い）で記録する。
  `DataCoverage::criticalGapsFor()`には含めておらず、
  `data:catch-up:status`の判定には一切影響しない。
- `CaptureBeforeInfoJob`は`tries=2`で、2回失敗すると`failed_jobs`に
  静かに入るだけでアラートは出ない。捕捉できなかったレースはstage1の
  ままとなり、ユーザーからは「このレースだけ直前情報反映バッジが
  付かない」という形でしか見えない。
- 日中に予測がstage1→stage2へ差し替わるため、軸艇(confident_top3)等が
  朝の時点と変わる可能性がある。何も示さずに差し替わるのは不親切なため、
  レース詳細(`RaceDetail.vue`)に`uses_before_info`に基づく注意書きを
  表示している。
- ブラウザでの目視確認は未実施（この環境にブラウザ自動操作ツール
  （built-in browser/Claude in Chrome/computer-use のいずれも）が
  無かったため）。`npm run build`の成功と、APIレスポンスの直接検証
  （`uses_before_info`等、テンプレートが参照する全フィールド）により
  代替した。

### 8. 変更・新規ファイル一覧（2026-10-08）
- 新規マイグレーション:
  `database/migrations/2026_10_08_100001_add_stage2_prediction_race_count_to_data_coverage_table.php`
- 新規コマンド: `app/Console/Commands/PredictionsGenerateStage2.php`
- 新規Vueコンポーネント: `resources/js/components/BeforeInfoBadge.vue`
- 新規モデルファイル: `ml/models/v5_binary_20261008.pkl` /
  `ml/models/v5_top3_20261008.pkl`
- 変更（Laravel側）:
  `routes/console.php`（beforeinfo:schedule-today再開、
  predictions:generate-stage2のeveryMinute+withoutOverlapping登録）、
  `config/ml.php`（prediction_stage2_model_version等）、
  `.env`/`.env.example`（PREDICTION_STAGE2_MODEL_VERSION等）、
  `app/Console/Commands/TicketsGenerateToday.php`（--stage追加）、
  `app/Console/Commands/DataCatchUp.php`（stage2_prediction_race_count
  のレポート追加）、`app/Models/DataCoverage.php`
  （stage2_prediction_race_count集計）、`app/Models/Race.php`
  （stage2Prediction/stage2Top3Prediction/effectivePrediction/
  effectiveTop3Prediction/usesBeforeInfo）、
  `app/Http/Controllers/Api/RaceController.php`（stage2のeager load）、
  `app/Http/Controllers/Api/PerformanceController.php`
  （active_winner/active_top3 CTEへの全面書き換え）、
  `app/Http/Resources/RaceSummaryResource.php` /
  `RaceDetailResource.php`（effectivePrediction経由・uses_before_info追加）
- 変更（ml側）: `ml/src/ml/models/train.py`（--feature-set追加、
  MODEL_PREFIXES）、`ml/src/ml/models/predict.py`（feature_set自動判定、
  --only-before-cutoff、v5 SQL）、`ml/src/ml/features/exhibition.py`
  （--race-ids追加）
- 変更（フロントエンド）: `resources/js/pages/RacesToday.vue` /
  `RaceDetail.vue`（BeforeInfoBadge表示、直前情報反映の注意書き）

## stage2初回稼働の実態・バッチ監視の欠陥・2件の実バグ修正（2026-10-10）

2026-10-09朝、`data:catch-up:status`が「当日races/predictions/top3_predictions
未取得」という誤報を出した件を調査したところ、バッチ自体は正常だったが
（前回のCLAUDE.md更新は誤報の原因調査までで止めていた）、調査の過程で
**無関係な実バグ2件**と**監視コマンド自体の設計欠陥**が見つかった。
いずれも実数で検証済み。

### 1. stage2構成「2026-10-08導入」は記録として不正確だった

`beforeinfo:schedule-today`を再有効化したコミット(`a9c26a9`)は
**2026-10-08 07:54 JST**に作成されていた。この日の実行枠は06:05 JSTで、
既にその枠を過ぎた後のコミットだったため、`storage/logs/schedule.log`を
確認すると`beforeinfo:schedule-today`が実際に初めて実行されたのは
**2026-10-09 06:05 JST（1回のみ）**だった。つまり**stage2(v5)が実データで
稼働した最初の日は2026-10-08ではなく2026-10-09**。上記「stage2構成の
導入（2026-10-08）」セクションは設計・判断根拠の記録として訂正の必要は
ないが、「2026-10-08から稼働した」という日付の理解は誤りだったため
ここで訂正する。

### 2. 2026-10-09(stage2初回稼働日)の実数

- races総数: **144**
- `race_before_info.source='live'`で捕捉できたレース数: **107**
  （874の候補の内訳は後述の33+4と合わせて144で一致）
- `CaptureBeforeInfoJob`の失敗（`failed_jobs`、race_id単位でdistinct、
  リトライは`tries=2`で1行に収束するため元々重複なし）: **33件、
  全件が下記2の文字エンコーディングバグ（内訳: encoding=33 / 締切超過=0 /
  その他=0）**。失敗率 = 33/144 ≈ **22.9%**。
  - **訂正**: 2026-10-09時点で一度「75レースで発生」と記録したが誤り。
    `failed_jobs`を`displayName`で`App\Jobs\CaptureBeforeInfoJob`に
    絞らず集計していたため、無関係な`App\Jobs\CaptureOddsJob`の失敗88件
    （別原因、調査対象外）を誤って合算していた。正しくは
    `CaptureBeforeInfoJob`のみで33件、かつ全て2026-10-09の1日に集中
    （上記1の通りこの日が実質の初回稼働日のため）。
  - `BeforeInfoScraper.php`のコメントも「発生頻度・再現性ともに低い」と
    書いていたが誤りで、実数(22.9%)に基づき訂正した。mb_scrub()による
    対処自体の妥当性は頻度の多寡によらず変わらない（個別原因調査より
    「不正なバイト列が来ても1文字単位で安全に失う」防御の方が実効的）。
- 残り4レース（stadium=21のR9〜R12、締切21:41〜22:54 JST）は
  `race_before_info`にも`failed_jobs`にも存在せず、ログを辿ると
  `CaptureBeforeInfoJob`自体が**翌朝04:51:06 JST(2026-10-10)**に実行され
  「`already passed; skipping`」で正常終了(成功扱い)していた。原因は
  下記4のPC電源off運用によるキュー処理の遅延で、締切(T-12分予約)から
  実際の実行まで6時間以上のギャップが生じ、その間に締切を過ぎてしまった
  ため。ジョブ自体は失敗していない(例外を投げず正常にreturn)ので
  `failed_jobs`には現れない。このケースはエンコーディングバグとは
  無関係の別カテゴリ（上記失敗率33/144にこの4件は含まれない）。
- stage2(v5)のpredictions: `data_coverage.stage2_prediction_race_count`=
  **107**、`predictions`テーブルの`model_version='v5_binary_20261008'
  AND stage=2`も`v5_top3_20261008 AND stage=2`も共に**107レース**で完全に
  一致。`Race::usesBeforeInfo()`がtrueを返すレースも107件で一致。
  **ライブ取得できた107レース分は、winner/top3ともstage2予測が正しく
  生成されていた**（捕捉できたのにstage2が書かれない、という別の不具合
  は無かった）。107(捕捉成功)+33(エンコーディング失敗)+4(締切超過)=144で
  races総数と完全に一致し、全レースの内訳に矛盾は無い。

### 3. 実バグ①: Kファイルのローカルキャッシュが「レース未終了時点の不完全な
　　スナップショット」のまま固定される

`ml/src/ml/fetchers/mbrace.py::fetch_and_extract()`は、`dest_dir`に
展開済み`.TXT`が既に存在すれば**ダウンロードそのものをスキップして
そのファイルを返す**（`force=True`でのみ再取得）。この仕組みはB(番組表)
ファイルには正しい（公開後に内容が変わらない）が、**K(結果)ファイルは
その日の開催が進むにつれて逐次更新され、最終版になるのは全レース終了後**
という前提が成り立たない。

実際に2026-10-09 18:32 JST頃（`data:catch-up --days=3`を手動実行した時点、
その時点で144レース中96レースしか終わっていなかった）に`K261009.TXT`が
`race_results`96レース分(576/864行)のみの不完全な状態でローカルに
キャッシュされ、以降（当日夜の自動実行は後述4の理由で発火せず、翌朝
2026-10-10 04:51の起動時`data:catch-up`でも）**同じ不完全なファイルを
読み直すだけで`has_results`/`has_payouts`が永久にfalseのまま**になって
いた。`data:catch-up:status`の当初の誤報を追跡する過程で発覚。

対応: `app/Console/Commands/DataCatchUp.php::fillDate()`の
`ml.loaders.cli load-results`呼び出しに`--force`を追加（既に
`cli.py`側に`--force`フラグ自体は実装済みだったが、PHP側から渡していな
かった）。この呼び出しは`has_results`または`has_payouts`が欠損している
時だけ実行される経路のため、強制再取得のコストは実際に欠損している数日分
に限られる。修正後、`php artisan data:catch-up --days=2`を再実行し
`K261009.TXT`が再取得され`race_results`864/864行・`payouts`1440件に
復旧、`has_results`/`has_payouts`ともtrueに反転したことを確認済み。

### 4. 実バグ②というより運用実態: 23:20の夜間ジョブはベストエフォートに
　　過ぎず、本当の安全網は起動時catch-up

`journalctl --list-boots`でPCの起動/終了履歴を確認したところ、
シャットダウン時刻は日によって**13:48〜21:14 JSTと大きくばらついており、
前日(2026-10-06〜09)は4日連続で23:20より前（20:26・21:14等）に電源が
落ちていた**。つまり前回(2026-10-09)追加した「23:20の`data:catch-up
--days=2`」は、**PCがその時刻にたまたま起動していない限り一度も実行され
ない**。今回2026-10-09夜もこれに該当し、実際に一度も発火しなかった
（`schedule.log`に該当行が無いことで確認済み）。
本当の安全網は起動時に必ず走る既存のsystemd(`boatrace-catchup.service`
→`data:catch-up --days=14`デフォルト)であり、上記3の`--force`修正と
合わせて初めて機能する。23:20ジョブは「PCがたまたま起動していた夜に限り
翌朝を待たず早く埋める」おまけの位置づけと理解すること（`routes/
console.php`のコメントに追記済み）。可用性向上という観点では、23:20の
時刻調整に意味は無い（シャットダウン時刻に安定した傾向が無く、どの時刻を
選んでも同程度に当たり外れがある）。

### 5. `data:catch-up:status`自体の設計欠陥

`data:catch-up:status`は`data_coverage`を**読むだけ**で、判定前に
再集計していなかった。`data_coverage`は`data:catch-up`（起動時のみ、
または上記4の通り不安定な夜間ジョブ）が更新するため、その実行以降に
状態が変化していても反映できない。2026-10-09朝の誤報
（起動時05:36 JSTのスナップショットのまま、06:00 JSTの
`races:fetch-today`以降の変化が反映されず「races未取得」と誤判定）は
この欠陥が直接の原因だった。より危険なのは逆方向で、**正常時に
書かれたスナップショットの後に何かが壊れてもOKを返し続ける**ため、
監視として機能しない期間が生じ得る。

対応: `app/Console/Commands/DataCatchUpStatus.php`で、判定前に必ず
`DataCoverage::refreshCoverage()`を前日・当日の2日分だけ呼ぶよう変更
（`status`が見るのはこの2日だけなので十分）。`refreshCoverage()`は集計
SQLの実行のみで外部アクセスを行わないため、頻繁に呼んでも副作用が無い
ことを確認済み（実測0.12秒で完了）。あわせて`data_coverage.updated_at`
を出力に含め、古いスナップショットを見ていないことが目視で分かるように
した。

### まとめ

今回の一連の調査で見つかった問題は、互いに独立した4つ（事実誤認1件・
実バグ2件・監視欠陥1件）だった。偶然同じ日に重なって顕在化したが、
原因は異なるため個別に修正・記録した。stage2の予測生成自体
（`PredictionsGenerateStage2`、v5モデルの推論ロジック）は正しく動作して
おり、ライブ取得できた107レース全てで正しくstage2予測が書かれていたこと
も確認済み。問題は「ライブ取得の成功率(22.9%の失敗)」「取得後のデータの
恒久的な固着(Kファイルキャッシュ)」「監視の鮮度(status未更新)」という、
stage2本体の外側にある3層の運用基盤側に集中していた。

## CaptureOddsJobの失敗88件の調査とオッズ取得設定の修正（2026-10-10）

上記の調査でbeforeinfoの失敗率をCaptureOddsJobの失敗88件と誤って合算した
ため（「stage2初回稼働の実態」参照）、別途CaptureOddsJob自体の88件を
調査した。

### 調査結果: beforeinfoと同じエンコーディング問題ではなかった

`failed_jobs`を`App\Jobs\CaptureOddsJob`に限定して88件を分類:
- `OddsFetchException: 3連単オッズテーブルが見つかりません`: **86件**
- Guzzleのネットワークタイムアウト(`ConnectTimeoutException`/
  `NetworkTimeoutException`): **2件**
- UTF-8不正バイト列: **0件**

UTF-8エラーは一致しなかった。86件は該当レースに突き合わせたところ
**全43レース(×2回=86件)が`races.cancelled=true`**（2026-09-21: 三国1R・
津6R・江戸川12R・戸田12R、2026-09-22: 津12R。CLAUDE.md「中止レースを
考慮したhas_results/has_payoutsの修正」で記録済みの荒天中止と一致）。
中止・不成立レースはodds3tページ自体は表示されるが（"データがありません"
にはならない）オッズテーブルが無いため、`TrifectaOddsScraper`が正しく
例外を投げていた。**異常ではなく構造的に避けられない失敗**。
残り2件は単発のネットワークタイムアウト。

- 稼働期間(odds_snapshots最古のcaptured_at基準、2026-09-18〜10-09)の
  総レース数3,408、想定ジョブ数(T-10分/T-5分の2回)6,816に対し失敗率
  **1.29%**。
- 中止レース(47件)を除いた「完走すべきだったレース」だけで見ると、
  失敗はタイムアウト2件のみで失敗率 **0.03%**。実質ほぼ問題なく稼働。

### odds_snapshotsはどこからも読まれていない

grepで確認: `odds_snapshots`を実際にクエリしているのは書き込み
(`CaptureOddsJob`)と監視目的の件数記録(`DataCoverage`/`DataCatchUp`、
`odds_race_count`)のみ。features/models/API/frontendのいずれからも
一度も読まれていない（`ml/src/ml/cutoff.py`のodds_snapshots言及は
docstring内の汎用的な例示のみで実際にクエリしていない）。回収率計算は
`payouts`を使っており無関係。2026-10-08のオッズ調査（市場の暗黙確率と
モデルの比較）は一回限りの手動分析で、リポジトリに保存されたスクリプト
としては存在しない。

### それでも取得は継続する（止めない）判断

- オッズは締切後に再取得できない。止めれば当該期間分は永久に失われる、
  不可逆な決定になる。
- 維持コストは1日あたり約300リクエスト（1レースにつき2回×144〜168
  レース/日）。既に実施した全期間backfill(beforeinfoの実績: 0.39 req/s
  を約120時間)と比べて無視できる規模。
- 2026-10-08の分析は1,843レース・約2.5週間という弱い標本であり、
  「統合の見込みは低い」という結論自体は維持するが、不可逆な決定
  （取得停止）の根拠として十分な強さではない。
- 結論: **取得は継続。ただし現状「貯めても使えない」状態だったため、
  使える形に直す**（以下2点）。

### 修正1: 取得タイミングをT-10分→T-13分に変更（リーク検証を通すため）

旧T-10分はcutoff_at(=締切10分前)と同値を狙っていたが、ジョブの実行自体が
キュー経由で数秒〜数十秒遅延するため、実測(1,933レース)で**captured_atが
cutoff_atより前になったことは0件**だった（必ず後になる＝リーク防止を
満たせず特徴量に使えない）。`app/Console/Commands/ScheduleOddsCapture.php`
の`[10, 5]`を`[13, 5]`に変更（3分の遅延マージンを確保し、cutoff_atより
確実に前の値として将来の特徴量候補にできるようにした）。T-5分（締切直前の
最終オッズ、研究用の参照値）はそのまま維持。リクエスト数は変わらず
1レースあたり2回。確認方法（翌日以降のデータでcaptured_at <=
cutoff_atを満たすか検証するSQL）は`ScheduleOddsCapture.php`のdocコメントに
残した。

### 修正2: 中止レースの失敗をfailed_jobsに残さない（監視を機能させる）

`App\Services\Boatrace\OddsTableNotFoundException`
(`OddsFetchException`のサブクラス)を新設し、`TrifectaOddsScraper`の
「オッズテーブルが見つからない」経路だけをこの型にした（HTTP失敗・
"データがありません"・行数不正・組み合わせ数不正・重複検出は従来通り
`OddsFetchException`のまま、本当の異常として区別を残す）。
`CaptureOddsJob::handle()`で`OddsTableNotFoundException`だけをcatchし、
ログに残して例外を投げずに正常終了させる（リトライさせず、
failed_jobsにも残らない）。ネットワークタイムアウト等の本当の失敗は
従来通り`tries=2`のリトライを経てfailed_jobsに残る挙動を維持。
中止レース起因の失敗が`failed_jobs`の大半(86/88=98%)を占めていたことで、
実際に今回beforeinfoの失敗率調査を一度誤らせた（CaptureOddsJobの失敗を
誤って合算）ため、「本当の失敗だけがfailed_jobsに残る」状態にする
意味は大きい。

## walk-forward検証の導入とv5の有意性判定（2026-10-09）
- オッズ調査(2026-10-08)で「市場がモデルを的中率0.27pt上回る」という結果が
  出たが、標準誤差1.15pt(0.23SE)で誤差の範囲だった。単一の時系列split
  (学習〜2025-12-31/検証2026-01-01〜09-17)ではこの規模の差を判定する検出力が
  無いため、今後の施策評価（v5特徴量・オッズ統合等）の土台としてwalk-forward
  検証を`ml/src/ml/models/walk_forward.py`に実装した。

### fold設計
- **学習窓: 固定開始(2023-09-01)のexpanding window**を採用（固定幅rolling
  windowは不採用）。理由: 1) 本番モデルは常に全履歴を学習に使う運用方針で
  あり、この方針と一致させた方が各foldの結果が本番運用の近似になる。
  2) 固定幅にすると初期foldの学習データが大きく削られるが、競艇のレース
  構造が3年間で大きく変化した根拠が無く、古いデータを捨てる積極的理由が
  無い。3) 欠点（foldごとに学習データ量が変わり、学習量の違いが性能差に
  混入しうる）は認識済み（後述「学習データ量との相関」参照）。
- **検証窓: 3ヶ月、重複なく前進。fold数: 6**。データ全期間
  (2023-09-01〜2026-09-30、2026-10-01以降は結果未確定のため対象外)37ヶ月
  のうち18ヶ月を検証窓に充て、最初のfoldでも19ヶ月の学習データを確保
  （本番の学習期間28ヶ月に対し見劣りしない下限と判断）。
- fold4の学習窓(2023-09-01〜2025-12-31)は本番モデルの学習期間と完全一致、
  検証窓(2026-01-01〜03-31)は本番検証期間(2026-01-01〜09-17)の最初の3ヶ月に
  相当し、既存の単一split記録値との整合性チェックに使える。

### 評価指標の統一（レース単位の多クラス式を正とする）
- is_winner(1着、6艇中1艇のみ正)は相互排他的な6値分類であり、
  `ml.models.lgbm.evaluate()`が実装する**レース単位の多クラス式**（log loss=
  実際の1着艇に割り当てた確率のみのcross entropy、Brier=6艇分の
  (予測確率-実際)^2の合計をレース単位で平均）が正しい定義である。これが
  既存の全記録（例: v1+v2+v3+v5で検証期間log loss=1.1799）のスケール。
  一方、オッズ調査やtop3.pyで使う**艇単位の二値log loss**(0.32前後の
  スケール)はis_top3（3着以内、3艇が独立に正）には正しいが、is_winner
  （6艇中必ず1艇だけが正）に使うと5艇の「外れた」確率分まで加算してしまい
  別の指標になる。両者は直接比較できない。walk_forward.pyは
  `lgbm.evaluate()`を唯一の算出箇所として呼び、重複実装しない。

### v3(v1+v2+v3) / v5(v1+v2+v3+v5)のfold別ベースライン
- `ml.models.baseline.dummy_lane1_hit_rate()`（常に1号艇を1着予測した場合の
  的中率=その期間の1号艇勝率）も併算し、
  `advantage = 的中率 - lane1率`（期間要因を相殺したモデルの純粋な上乗せ）を
  主指標に追加した。

  | fold | 検証窓 | v3的中率 | v5的中率 | lane1率 | v3優位 | v5優位 | v3 log loss | v5 log loss |
  |---|---|---|---|---|---|---|---|---|
  | 1 | 2025-04〜06 | 54.96% | 55.03% | 53.06% | +1.90pt | +1.97pt | 1.2292 | 1.2123 |
  | 2 | 2025-07〜09 | 54.79% | 55.18% | 52.89% | +1.90pt | +2.29pt | 1.2293 | 1.2135 |
  | 3 | 2025-10〜12 | 56.30% | 56.63% | 54.78% | +1.53pt | +1.85pt | 1.2106 | 1.1913 |
  | 4 | 2026-01〜03 | 56.00% | 56.63% | 54.36% | +1.64pt | +2.27pt | 1.2012 | 1.1789 |
  | 5 | 2026-04〜06 | 56.12% | 56.90% | 54.32% | +1.80pt | +2.58pt | 1.1887 | 1.1723 |
  | 6 | 2026-07〜09 | 56.38% | 56.70% | 54.15% | +2.23pt | +2.55pt | 1.1992 | 1.1840 |
  | **平均±SD** | | **55.76±0.70pt** | **56.18±0.84pt** | **53.93±0.77pt** | **+1.83±0.24pt** | **+2.25±0.29pt** | 1.2097±0.0167 | 1.1921±0.0173 |

- **v3/v5とも優位(advantage)のSD(0.24pt/0.29pt)は的中率のSD(0.70pt/0.84pt)の
  1/3程度**。fold間変動の大部分は期間要因(lane1率の変動、SD0.77pt)であり、
  モデル自体の実力はそれよりずっと安定している。
- **既存の単一split記録(的中率56.20%/56.69%、ダミー54.26%に対し+2.43pt)
  との関係**: 単一splitの検証期間(2026-01-01〜09-17)はfold4〜6に相当し、
  そのlane1率平均は54.28%で既知値54.26%とほぼ一致。数値自体は妥当だが、
  **「ダミーに対する優位」は期間により+1.5〜2.2pt(v3)・+1.9〜2.6pt(v5)の
  範囲で変動する**ことがfold別の結果から分かった。単一splitの+2.43ptという
  値を「モデルの恒常的な優位性」の代表値として扱わないこと。

### v5がv3を有意に上回ると判定
- v5-v3の的中率差は**全6fold中6fold(100%)で改善**（符号の逆転なし）。
  平均+0.42pt、標準偏差0.25pt、標準誤差(平均)0.10pt(n=6)。
  有意性の目安(両側95%, t(df=5)=2.571): |平均差|>0.26pt。**+0.42ptはこれを
  上回り、有意な改善と判定**。log loss・Brierも全foldで一貫して改善。
- advantageベースで同じ比較をしても、**同一fold内でlane1率の項が代数的に
  相殺するため、raw的中率ベースと数値的に完全に一致する**
  (advantage_v5 - advantage_v3 = (hit_v5-lane1) - (hit_v3-lane1) =
  hit_v5-hit_v3)。advantageの価値はこの比較の検出力向上ではなく、fold単体の
  解釈性（期間要因とモデルの実力を分離できる）と段差の原因診断にある
  （後述「lane1勝率の構造変化」参照）。
- **【訂正: 2026-10-09追記】「0.26pt」は固定の判定基準ではない**:
  当初この値を恒久的な閾値であるかのように記録したが誤りだった。
  正しくは、**0.26ptは「v5 vs v3」という特定の比較におけるペア差の標準偏差
  (0.25pt)から、その場で算出された実例値**に過ぎない。目安は
  `t(df=5)=2.571 × 標準誤差(=ペア差の標準偏差/√n)`で**比較ごとに毎回
  算出し直す**ものであり、比較対象の性質によって大きく変わる。実際、
  時間減衰重みの検証（後述）では同じ6foldなのに次のように変動した:
  - v5 vs v3: ペア差SD 0.25pt → 目安0.26pt
  - 減衰6ヶ月 vs 重みなし: ペア差SD 0.16pt → 目安0.16pt
  - 減衰24ヶ月 vs 重みなし: ペア差SD 0.08pt → 目安0.08pt

  比較対象同士が似ている（同じ特徴量・ほぼ同じ学習データで、片方が
  重み付けだけ違う等）ほどペア差のばらつきは小さくなり、目安は厳しく
  （小さく）なる。**正しい運用ルールは「比較するたびに
  `walk_forward.py`の出力からその場の目安を読み、かつ全fold一貫を
  要求する」であり、「0.26pt」という数値そのものを他の比較に使い回しては
  いけない**。
- この基準は各foldが独立であることを前提にしているが、expanding window
  設計ではfold間に学習データの重複がある（例: fold4の学習窓にはfold1〜3の
  検証窓がそのまま含まれる）。この重複により誤差が正の相関を持ちうるため、
  通常のt検定は実際より自信過剰な(甘い)判定になる可能性がある。そのため
  単一の閾値判定だけでなく、**「全fold一貫して同方向」という条件を必ず
  併用する**運用ルールとする（両方が揃って初めて「有意」と呼ぶ）。
- 学習データ量(train races)とv5-v3の的中率差の相関係数は**+0.573**
  (n=6)。95%棄却に必要なr(約0.81)に届かず統計的に有意ではない。加えて
  expanding window設計ではtrain racesはfoldが進むほど暦時間とともに単調
  増加するため、「データ量の効果」と「暦時間の経過に伴う効果(季節性等)」を
  本設計では分離できない。**現時点では偶然の範囲として扱う**。

### lane1勝率の構造変化（2025-10〜11頃）の調査
- fold2(2025-07〜09、52.89%)とfold3(2025-10〜12、54.78%)の間の段差
  (+1.89pt)について、月次で細分化して原因を調査した。

  | 月 | lane1率 | 月 | lane1率 |
  |---|---|---|---|
  | 2025-05 | 52.45% | 2025-09 | 52.81% |
  | 2025-06 | 52.21% | 2025-10 | 53.23% |
  | 2025-07 | 52.23% | **2025-11** | **56.36%** |
  | 2025-08 | 53.64% | 2025-12 | 54.81% |

  2025-05〜10の6ヶ月は52.2〜53.6%で安定して低く、**2025-10→11の1ヶ月で
  53.23%→56.36%(+3.13pt)と急激に変化**しており、緩やかな移行ではなく
  2025年11月を境にした段階的な変化に見える。2025-11以降は2026-09まで
  53.5〜56.4%のレンジで高止まりし、以前の水準(52〜53.6%)には戻っていない
  （pre-shift(2025-05〜10, 28,620レース): 52.75%、post-shift(2025-11〜
  2026-09, 51,468レース): 54.49%、差+1.74pt）。
- **候補検証（手元データ、2025-05〜2026-02で確認）**:
  - 進入固定率(lane1のstart_course==1の比率): 97.1〜98.5%で安定。2025-11も
    98.13%で前後と有意差なし。**シフトなし**。
  - 1号艇の平均ST: 0.148〜0.157で安定。2025-11は0.149で平均的。
    **シフトなし**。
  - フライング/出遅れ件数: 月14〜39件/22件で小さくノイジー、2025-11は
    flying=14件とむしろ少ない方。**系統的なシフトなし**。
  - 1号艇のA1級比率: 29.9〜36.6%で変動。2025-11は36.6%と局所的に高いが、
    2025-12は30.9%に戻り、勝率は54.81%のまま高止まり。**持続的な説明には
    ならない**（11月単月への寄与はあり得るが、12月以降の高止まりは
    説明できない）。
  - 距離構成(1200m/1800m比率): 1800mが96%以上を占め続け大きな変化なし。
  - 決まり手(逃げ比率): `race_results`に決まり手列が無く直接検証不可。
    進入固定率(上記)が代理指標になるが、そちらにシフトは無い。
  - 気象(風速・波高): `race_weather_info`はbeforeinfo由来で2026-06-21以降
    のデータしか無く、**2025年分は検証不能**（データ欠如、シフトなしの
    確認ではない点に注意）。
  - 以上、**手元データで確認できた候補はいずれも2025-10/11の変化を
    説明しない**。
- **外部要因（web検索）**: 日本モーターボート競走会が、転覆落水事故防止策
  として旋回後期の安定性を高めた改造キャビテーションプレート搭載モーターを
  **2025年9月5日のボートレース唐津より競走用として導入開始**し、以後
  「全国のボートレース場でモーターを新しいものに切り替える際に順次導入」
  する方針であることが確認できた
  （[boatrace.jp公式発表](https://boatrace.jp/owpc/pc/site/news/2025/09/45310/)）。
  公式見解では旋回性は「従来と同等かそれ以上に安定」で性能への影響は
  想定されていないが、**旋回後期の安定性向上という効果の性質上、
  ターンでの追い抜き（差し/捲り）を難しくし先行艇(多くは1号艇)に有利に
  働く可能性**は理屈の上では排除できない。全国順次導入という時期（2025年
  9月開始）は観測されたシフト（2025年10〜11月）と近接しており、かつ
  この仮説は「スタート前(進入・ST)には現れず、ターン後の勝敗だけに
  現れる」という手元データの観測（進入固定率・STにシフトなし、勝率にのみ
  シフトあり）と整合する。**ただし因果関係は確認できておらず、
  相関関係の候補の一つに留まる**（各場の導入時期の個別データが無く、
  直接検証はできていない）。
- **特徴量化の可能性**: 手元データで確認できた候補（進入固定率・ST・
  フライング率・A1比率・距離構成）はいずれも安定しているかシフトと無関係な
  変動のため、新規特徴量を追加する動機は無い。外部要因候補（モーター
  改修）は、場ごとの導入日という現在のパイプラインには存在しないデータが
  必要で、boatrace.jpの個別発表から手作業で場別導入日を集めない限り
  特徴量化できない。現時点では着手しない。
- **結論**: 2025年10〜11月のlane1勝率上昇は、手元データで確認できる要因
  （進入・スタート・選手構成・距離構成）には帰着しなかった。モーター改修
  という外部要因は時期・作用機序の両面で整合的な候補だが未確認。また、
  2023-09〜2024-12の月次推移でも同程度(2pt超)の単月変動が繰り返し
  発生しており（例: 2024-03→04で+2.52pt、2024-08→09で+2.22pt、いずれも
  既知の外部要因なし）、**今回のシフトが「前例のない構造変化」と言い切れる
  ほどの根拡もない**点は留意する。walk-forwardのfold設計・有意性判定の
  手法自体は上記の通り確立できたため、原因の特定自体は本タスクの主目的
  ではなく、今後類似の段差が出た際の切り分け手順（月次分解→候補検証→
  外部要因調査）の実例として記録する。

### 時間減衰サンプル重みの検証（2026-10-09）— 効果なし
- lane1勝率の構造変化を原因から特定するより、「最近のデータを重視すると
  モデルが良くなるか」を直接測る方が速いと判断し、学習時のサンプル重みに
  時間減衰を導入できるようにした上でwalk-forwardで検証した。
- **実装**: `ml.models.lgbm.compute_time_decay_weights()`を追加。
  reference_date(通常は各foldのtrain_end)に近いrace_dateほど重みが大きい
  サンプル重みを、半減期(half_life_days)1つで3つの減衰形に統一的に
  パラメータ化する:
  - `exponential`: weight = 2^(-age/half_life)（連続的に減衰）
  - `linear`: weight = max(0, 1 - age/(2*half_life))（2*half_life以降は0）
  - `step`: weight = 2^(-floor(age/half_life))（half_life周期で階段状に半減）

  `lgbm.train_model()`/`top3.train_top3_model()`に`sample_weight`引数
  （デフォルトNone=重み無し、**現行と完全に同じ挙動**）を追加し、
  `lgb.Dataset(weight=sample_weight)`にそのまま渡す。
  `fetch_all_dataset()`/`fetch_v5_dataset()`に`race_date`列を追加した
  （重み計算に必要。既存の呼び出し元は`feature_columns`だけを明示selectする
  ため影響なし）。
- `ml/src/ml/models/walk_forward.py`に`--weight-sweep`モードを追加。
  train_df/val_dfのfetchをfold毎に1回だけ行い、複数の重み設定で学習だけを
  繰り返すことで（DBアクセスをN回に増やさず）、半減期候補と重み無しを
  同条件で比較できるようにした。
- **検証**: v1+v2+v3のみ（v5を含めない。v5のカバレッジ問題と減衰の効果を
  混ぜないため）、半減期6/12/18/24ヶ月(exponential、1ヶ月=30日換算)を
  重みなしと6fold全てで比較。判定基準は上記「0.26ptは固定基準ではない」の
  通り、**比較ごとにその場で算出した目安 かつ 全fold一貫**。

  | 半減期 | 平均差 | 目安(その場で算出) | 全fold一貫 | 判定 |
  |---|---|---|---|---|
  | 6ヶ月 | +0.01pt | 0.16pt | False(改善4/悪化2) | 効果なし |
  | 12ヶ月 | +0.02pt | 0.15pt | False(改善5/悪化1) | 効果なし |
  | 18ヶ月 | +0.02pt | 0.15pt | False(改善4/悪化2) | 効果なし |
  | 24ヶ月 | +0.01pt | 0.08pt | False(改善4/悪化2) | 効果なし |

  全候補で平均差はほぼゼロ(+0.01〜0.02pt)、かつ全fold一貫の条件を
  満たさず、**確立した判定ルールで「効果なし」**。log loss/Brierの差も
  ±0.002台で無視できる水準。
- **fold1だけ全候補で悪化(-0.13〜-0.28pt)した点**: fold1は学習窓が
  2023-09-01〜2025-03-31の19ヶ月で、6fold中もっとも学習データが少ない
  （88,068レース）。学習データが少ない状態でさらに古いデータの重みを
  下げると、実質的な有効サンプルサイズがより大きく削られるため、
  重み付けの悪影響（データ量減少によるノイズ増加）が他のfoldより強く
  出たと考えられる。データ量に余裕がある他のfold（102,228〜157,884
  レース）では、この悪影響が相対的に薄まり、むしろ微改善する方向に
  振れたものの、「全fold一貫」を満たすほどの一貫性は無かった。
- **v1+v2+v3+v5での追試**: 有意と判定された半減期が1つも無かったため、
  未実施（計画では最良の半減期が見つかった場合のみ実施する想定だった）。
- **結論**: 学習データは、v1+v2+v3の特徴量を通した予測においては
  モデルが性能を変えるほど非定常ではない。lane1勝率の2025-11以降の
  高止まりは、実在するとしても、現行モデルの学習には実質的な影響を
  与えない程度の変動だったと結論できる（「原因不明」で終わらせず、
  「影響の有無」という上位の問いには答えが出た）。
- 実装（`compute_time_decay_weights`・`--weight-sweep`）自体は削除せず
  残してある。将来、別の特徴量セットや別のモデルで非定常性が疑われる
  場合に再利用できる。

### ハイパーパラメータの再探索（2026-10-09）— 2段階ともに効果なし
- 特徴量が34列(v1+v2+v3)から51列(v5込み)に増えた一方、`DEFAULT_PARAMS`は
  旧構成のままで、`num_boost_round=200`も全fold一律だった（学習データ量は
  fold1の88,068レースからfold6の157,884レースまで1.8倍差があるにも
  関わらず）。walk-forwardの枠組みが整ったので機械的に見直した。

#### 第1段階: early stopping
- `ml.models.lgbm.train_model_with_early_stopping()`を追加。学習窓の末尾
  90日(約3ヶ月)を内部検証セットとして切り出し、binary_loglossを監視して
  early stoppingで木の本数(best_iteration)を決める。内部検証は**学習窓の
  中だけで完結**し、walk-forwardの検証fold(評価対象)は一切参照しない
  （リークしない）。90日とした理由: fold1の学習窓が19ヶ月と最短のため、
  長すぎると学習データが大きく削られる。既存の検証窓自体が3ヶ月であり、
  長さを揃えることで1号艇勝率の月次変動のような短期ノイズをある程度
  均せる最小限の長さとして妥当と判断した。best_iterationを固定本数として
  train_df全体(内部検証に使った分も含む)で再学習し、最終モデルが内部検証
  のために学習データを失わないようにしている。
- 検証結果: fold別best_iterationは**100/134/190/177/280/160と2.8倍
  ばらついた**（200固定が全foldで最適だったわけではないことは判明）。
  にも関わらず性能差は平均-0.01pt、目安0.04pt以内、全6fold不一致
  （改善2/悪化4）で**「効果なし」**。
- **解釈**: 木の本数が100〜280という大きな幅のどこであっても性能が
  ほぼ変わらない、という事実は、「本数を最適化すれば性能が上がる」
  という期待とは逆に、**モデルが木の本数に対して鈍感である**ことを
  示している。200本固定のまま運用して実害はない。

#### 第2段階: ランダムサーチ
- `ml.models.walk_forward.sample_params()`/`run_random_search()`を追加。
  探索範囲(`PARAM_SEARCH_RANGES`):

  | パラメータ | 範囲 | サンプリング |
  |---|---|---|
  | num_leaves | 15〜255 | 一様(int) |
  | min_data_in_leaf | 10〜200 | 一様(int) |
  | feature_fraction | 0.5〜1.0 | 一様 |
  | bagging_fraction | 0.5〜1.0 | 一様 |
  | bagging_freq | 1〜10 | 一様(int) |
  | lambda_l1 | 10^-4〜10^1 | log10一様 |
  | lambda_l2 | 10^-4〜10^1 | log10一様 |
  | learning_rate | 10^-2〜10^-0.7(約0.01〜0.2) | log10一様 |

  num_boost_round(200固定、第1段階の結論に従う)は探索対象外とした
  （同時に探索すると「少ない本数+弱い正則化」と「多い本数+強い正則化」が
  区別できなくなるため）。乱数シード0、v1+v2+v3のみ（v5のカバレッジ問題を
  混ぜないため）、全6foldで評価。
- 上位5件（平均的中率）: **55.83% / 55.82% / 55.80% / 55.78% / 55.77%**。
  1位と2位の差+0.012pt、1位と5位の差+0.059ptと**はっきり団子状態**。
- 最良候補(num_leaves=238, min_data_in_leaf=140, feature_fraction=0.959,
  bagging_fraction=0.824, bagging_freq=7, lambda_l1=1.543,
  lambda_l2=0.0018, learning_rate=0.0536)のDEFAULT_PARAMSベースライン比:
  見かけの改善+0.070pt。しかしfold別は改善4/悪化2で**全fold一貫を満たさず**、
  その場で算出した目安0.14ptにも届かない。**不採用**。
- **多重比較の実例として重要**: もし上位5件の団子確認をせず「40試行中の
  最良値」だけを見て報告していれば、「ハイパーパラメータ探索で的中率が
  +0.07pt改善した」と誤って結論していたところだった。実際には40個の
  ノイズの中から偶然最大だった値を選んでいただけで、団子状態の確認
  （上位5件の差が0.059pt以内）と全fold一貫の要求が、この見かけの改善を
  正しく「誤差の範囲」と判定する決め手になった。

#### 3つのnullを総合した所見（重要）
- 時間減衰重み（半減期6/12/18/24ヶ月）・early stopping・ハイパーパラメータ
  ランダムサーチ(40試行)の**3つすべてが「効果なし」**という結果になった。
  best_iterationが2.8倍(100〜280)違っても性能がほぼ変わらず、
  ハイパーパラメータを変えても上位5件が0.059pt以内に団子で並ぶという
  事実は、**現在の51特徴量に対してモデルが学習手続き（木の本数・
  正則化・サンプリング比率等）に対して全般的に鈍感である**ことを
  示している。
- **結論: 1着的中率の残りの伸びしろは、学習方法の改良では得られない。
  新しい情報（特徴量）が必要**。今後、同種の「学習手続きの最適化」
  （さらなるハイパーパラメータ探索、別のboosting設定等）に時間を使う前に、
  この所見を判断根拠として参照すること。オッズ統合（CLAUDE.md「オッズ
  (odds_snapshots)を特徴量として使えるか」参照）のような、モデルの外から
  新しい情報を持ち込む施策の方が、現時点では有望な方向だと考えられる。
  **【2026-10-09追記】同日中にtop3モデルの正規化比較・ハイパーパラメータ
  探索も実施し、同様に不採用という結果になった（詳細は下記「top3モデルの
  正規化とハイパーパラメータ探索」参照）。これで時間減衰重み・early
  stopping・winnerモデル探索・top3モデル探索+正規化の**4つすべてが
  null**となり、「学習手続きの最適化は打ち止め」という結論はより強く
  裏付けられたと考える。**
- 実装（`train_model_with_early_stopping`、`--boosting-compare`、
  `--random-search`、`PARAM_SEARCH_RANGES`）は削除せず残してある。
  特徴量セットが大きく変わった場合（例: オッズ特徴量を追加した場合）は
  再度このツールで検証し直す価値がある。

## top3モデルの正規化とハイパーパラメータ探索（2026-10-09）— ともに不採用

confident_top3(実績90.78%、閾値0.84)は商品の中心機能だが、top3モデル
（is_top3直接学習モデル、CLAUDE.md「p_top3の直接学習モデル」参照）の
正規化方式とハイパーパラメータには一度も手を入れていなかった。2段階で
検証した。実装は`ml/src/ml/models/top3_normalization_experiment.py`
（新規）と`ml/src/ml/models/top3.py`に追加した関数群。

### 第1段階: 正規化方式の3方式比較

- 現行の正規化は「線形スケーリング（各艇の生シグモイド出力を合計3.0に
  なるよう比例配分）+ clip(0,1)」。合計が3.0を超える艇がある場合、
  比例配分後も1.0を超える値が残ることがあり、clip(0,1)で切り詰めている
  （「p_top3の直接学習モデル」に記載済みの、244,152件中1,318件=0.54%で
  発生するclip）。この仕組みを見直せないか検証した。
- 実装した3方式（同一boosterの生ロジット出力から算出。学習は1回のみ）:
  - A) 現行: 線形スケーリング + clip(0,1)（`predict_race_sum3_for_inference`）
  - B) 線形スケーリング（clipなし、`predict_race_sum3`の素の出力）
  - C) ロジット空間での平行移動（新規。`solve_logit_shift()`で
    Σ sigmoid(logit_i + δ) = 3.0 を満たすδを二分探索で求め、
    `sigmoid(logit_i + δ)`をそのまま艇iのp_top3とする。ベクトル化版は
    `_logit_shift_p_top3()`、本番向けインターフェースは
    `predict_race_logit_shift_for_inference()`で、既存の
    `predict_race_sum3_for_inference`と完全互換のシグネチャにしてある）。
  - Cの解の存在証明: f(δ)=Σsigmoid(logit_i+δ)はδについて連続・狭義単調
    増加で、δ→-∞で0、δ→+∞で6(艇数)に収束する。3.0は常にこの開区間
    (0,6)の内側にあるため、6艇のロジットがどれほど極端な値であっても
    中間値の定理により解は艇数・値によらず必ず一意に存在する
    （「解なし」は数学的に起こり得ない）。そのためCは値が構造的に(0,1)
    に収まり、clipが原理的に不要（実装上も常にclipped_count=0を返す）。
  - 実装上の注意: ベクトル化実装の初期版で、δ全体を二分探索で広げる
    while文の中の判定式が`1.0 / (1.0 + np.exp(...)).sum(axis=1)`と
    丸括弧を誤って付けており（正しくは
    `(1.0 / (1.0 + np.exp(...))).sum(axis=1)`）、`.sum(axis=1)`が
    シグモイド全体ではなく分母側にのみ掛かっていたため、極端なロジット値
    を持つテストケースで判定式の上限が構造的に3.0に到達できず、
    bracket拡張ループが無限ループした（`ps aux`でCPU使用率約101%に
    張り付いたプロセスを確認して発覚）。丸括弧を修正して解消。
- walk-forward(6fold、v1+v2+v3特徴量)でis_top3モデルをfold毎に独立に
  学習し、各foldの検証データを全6fold分プール(84,336レース)した上で、
  本番API定義のconfident_top3（レースごとにp_top3最大の1艇のみを候補に
  する方式）で閾値0.70〜0.99を0.01刻みで再スイープした。

  | 方式 | 閾値 | 該当数 | 実績 | キャリブレーション最大ズレ |
  |---|---|---|---|---|
  | A) 現行(線形+clip) | 0.84 | 46,357 | 90.06% | -1.07pt(最高位bin) |
  | B) 線形(clipなし) | 0.84 | 46,357（Aと完全一致） | 90.06% | -1.25pt(最高位bin) |
  | C) ロジット平行移動 | 0.84 | 45,839（-518件） | 90.27% | -1.17pt(2番目bin) |

  （いずれも「実績90%を保ったまま該当数を最大化する閾値」として0.84が
  選ばれた。walk-forwardプールでの数値であり、本番が使う単一splitの
  検証期間の数値（22,646件・90.78%）とは母集団が異なるため直接比較
  できない点に注意。）
- 順位保存の検証（`check_rank_preserved()`、レース内でp_top3降順に並べた
  時の艇順が2方式間で一致するかを全レースで確認）: A vs B 不一致
  1/84,336レース、B vs C 不一致 0/84,336レース（Cは単調変換のため理論上も
  必ず0になるはずで、実測と一致）。
- **結論: clipは実質無害だった。該当数・実績ともA/Bで差がなく、Cは
  現行Aよりわずかに該当数が減りキャリブレーションもやや悪化するため、
  3方式とも現行Aを置き換える理由がない。正規化方式の変更は不採用、
  現行Aを維持する**。

**事前の仮説が外れた経緯（診断の失敗例として記録）**: 着手前は
「clip対象1,318件(0.54%)が確信度最大の艇に集中しており、同値化で
confident_top3が参照する艇順の情報が失われているのではないか」と推測し、
この仮説を検証する目的でCの実装に着手した。しかし実際の順位不一致は
A vs Bで84,336レース中1件のみだった。理由は、clipが「1艇の値を1.0に
切り詰める」操作であっても、**レース内の艇順が変わるのは同一レースで
2艇以上が同時にclip対象になり、かつclip後に順位の大小関係が入れ替わる
場合だけ**であり、合計3.0という正規化制約の下では1レースに複数の
clip対象艇が同時に出現すること自体が極めて稀だったため。
「clip対象の発生率(0.54%、艇単位)」という周辺統計だけを見て、それが
そのまま「confident_top3という意思決定（レース単位の順位選択）への
影響」に直結すると見積もったのが誤りで、**周辺の発生率(艇単位)と実際に
意味のある単位(レース単位の順位)での影響は別物であり、後者を直接計測
せずに前者から推測してはならない**という教訓として記録する。
- 実装(`solve_logit_shift`/`_logit_shift_p_top3`/`predict_race_logit_shift`
  /`predict_race_logit_shift_for_inference`)は削除せず残してある。将来
  採用する場合は`ml/src/ml/models/predict.py`の呼び出しを
  `predict_race_sum3_for_inference`→`predict_race_logit_shift_for_inference`
  に変えるだけで切替できる（シグネチャ互換）。

### 第2段階: top3モデルのハイパーパラメータ探索（ランダムサーチ20試行）

正規化は現行Aのまま、`ml.models.walk_forward`の`PARAM_SEARCH_RANGES`/
`sample_params`（winnerモデルの探索で使ったものと同じ探索空間）を流用し、
is_top3モデルに対して20試行×6foldのランダムサーチを行った。主指標は
1着的中率ではなく「閾値を再スイープした上での実績90%以上を保てる該当数の
最大値」（confident_top3が実際に使う意思決定指標）。

- ベースライン(DEFAULT_PARAMS、方式A): 閾値0.84、該当数46,357、実績90.06%
  （1日あたり84.59艇、548検証日換算）。
- 上位5件（該当数降順、いずれも実績90%以上の条件下）:

  | 順位 | trial | 該当数 | 実績 | 閾値 |
  |---|---|---|---|---|
  | 1 | 8 | 46,481 | 90.16% | 0.84 |
  | 2 | 13 | 46,377 | 90.14% | 0.84 |
  | 3 | 6 | 46,271 | 90.02% | 0.82 |
  | 4 | 18 | 46,255 | 90.21% | 0.84 |
  | 5 | 11 | 46,151 | 90.16% | 0.84 |

  **ベースラインの46,357件は、この並びに挿入すると2位(46,377)と
  3位(46,271)の間、すなわち実質3位相当**に位置する。1位との差
  (+124件)は、上位5件内の散らばり(1位-5位で330件)より小さい。
- 最良候補(trial8、num_leaves=89, min_data_in_leaf=190,
  feature_fraction=0.562, bagging_fraction=0.666, bagging_freq=9,
  lambda_l1=0.00104, lambda_l2=0.993, learning_rate=0.0515): 該当数
  46,481件（ベースライン比+124件、+0.27%）、実績90.06%→90.16%。
  fold単体で独立にsweepした該当数でも**6fold中6fold(100%)で改善**
  （fold1: 7,738→7,806、fold2: 6,872→6,905、fold3: 6,084→6,647、
  fold4: 7,539→8,116、fold5: 8,758→8,773、fold6: 7,656→8,211）。

**「全fold一貫」を満たすにも関わらず不採用と判断した理由（2点）**:
1. ベースラインが候補群の中で既に上位（実質3位相当）に入っており、
   DEFAULT_PARAMSが探索空間の中で既にほぼ最良点にあることを示している。
   1位との差(+124件)は上位5件の散らばり(330件)に収まる大きさで、
   「改善」自体が探索のノイズ幅の範囲内と見るのが妥当。
2. 「全fold一貫」という基準を、事前に1つに決めた仮説ではなく**20試行
   という探索の中から事後的に選んだ最良候補**に適用する場合、この基準の
   検出力は大きく低下する。真に実質同等な（母集団として差がない）候補が
   偶然6fold全てで上回る確率は(1/2)^6 = 1/64 ≈ 1.6%だが、20試行の中に
   「少なくとも1件、たまたま6fold全勝する候補」が混ざる確率は
   1-(1-1/64)^20 ≈ 27.0%（4回に1回弱）であり、無視できない水準になる。
   つまりこの文脈での「6fold中6fold改善」は、見た目ほど強い証拠では
   ない。

### 判定ルールの2つ目の訂正: 「全fold一貫」は探索の勝者選びにそのまま使えない

前項（2026-10-09の早い時間に記録した「0.26ptは固定の判定基準ではない」
訂正、「ハイパーパラメータの再探索（2026-10-09）」節参照）に続く、
同日2つ目の訂正を記録する。
**「全fold一貫して同方向に改善」という基準は、事前に決めた単一の仮説
（例: v5 vs v3、特定の半減期1つを試す時間減衰重み）を検証する際には
有効な必要条件だが、複数の候補から事後的に選んだ「探索の勝者」に
そのまま適用すると機能しない。** 理由は上記第2段階の(2)の計算の通りで、
候補数が増えるほど「たまたま全fold一貫する候補」が出現する確率は
無視できない水準まで上がる（今回は20試行で約27%）。
**探索（ランダムサーチ等、複数候補から1つを選ぶ場面）での正しい運用は、
「全fold一貫」に加えて「ベースライン(現行設定)が候補群の中で何位に
位置するか」を必ず確認すること。ベースラインが既に上位（団子の中）に
入っている場合は、最良候補との差を探索ノイズと判断し、全fold一貫を
満たしていても採用しない。**（単一の事前仮説を2つだけ比較する場面
（例: 時間減衰重みの特定半減期1つ vs なし、winner/top3のA-B比較）には
「候補群の中の順位」という概念自体が存在しない＝比較対象が2つしかない
ため、この訂正はそうした比較には影響しない。影響するのはランダム
サーチのように3つ以上の候補から事後的に勝者を選ぶ場面に限られる。）

### 4つのnullを総合した所見（2026-10-09更新）

2026-10-09に実施した4つの検証——1) 時間減衰サンプル重み（半減期
6/12/18/24ヶ月）、2) early stopping、3) winnerモデルのハイパーパラメータ
ランダムサーチ(40試行)、4) top3モデルの正規化比較(3方式)+ハイパー
パラメータランダムサーチ(20試行)——**すべてが「効果なし/不採用」という
結果**に終わった。winnerモデルは木の本数が2.8倍(100〜280)違っても性能が
変わらず上位5件が0.059pt以内に団子、top3モデルは正規化を変えても
該当数・実績がほぼ変わらず、ハイパーパラメータ探索の上位5件も330件
（該当46,000件規模に対し0.7%）以内に団子で並ぶ。これらの事実は、
**学習手続き（サンプル重み付け・木の本数・正則化・サンプリング比率・
正規化方式のいずれであっても）をどう変えても、現在の特徴量セットから
引き出せる性能はほぼ天井に達している**ことを、1着予測・3着以内予測の
両方で一貫して示している。
**結論: 学習手続きの最適化はここで打ち止めとする。1着的中率・
confident_top3いずれも、残りの伸びしろは学習方法の改良では得られず、
新しい情報（特徴量）が必要という結論を変える理由はこれで無くなった**。
オッズ統合（CLAUDE.md「オッズ(odds_snapshots)を特徴量として使えるか」
参照）が現時点で最も有望な方向だと考えられる。

### 第6項: 本番投入の前提（参考情報、今回は未実施）

正規化を現行Aから将来C(ロジット平行移動)に変更する場合に必要になる
変更点を参考情報として残す（今回はAを維持するため以下は未実施）:
- `config/ml.php`の`top3_confident_threshold`（現0.84）をCの確率スケール
  で再スイープし直す（Cはしきい値0.97超でほぼ該当数が無くなるなど、
  高確率帯での分布がAと大きく異なるため、0.84という数値自体は近いが
  周辺の挙動は別物として再検証が必要）
- `top3_confident_threshold_options`（現`[0.80,0.84,0.88,0.92,0.96]`）も
  Cの確率スケールに合わせて再設定
- `resources/js/pages/RacesToday.vue`の「84%以上...実績90.4%」という
  ハードコードされた数値の更新
- `resources/js/pages/Performance.vue`の`confidentThreshold`デフォルト値・
  フォールバック選択肢の更新
- 「90%保証」の再検証を、本番が実際に使う単一split検証期間
  （2023-09-01〜2025-12-31学習 / 2026-01-01〜09-17検証）で正式に
  やり直す（今回のwalk-forwardプール(84,336レース・548日)は研究用の
  母集団であり、本番の判断根拠とする数値はこちらで取り直す必要がある）
- `ml/src/ml/models/predict.py`の呼び出しを
  `predict_race_sum3_for_inference`→`predict_race_logit_shift_for_inference`
  に切替（シグネチャ互換のためこの1箇所の変更で済む想定）

## 節間成績・体重差の新規性調査とv6_weight_diffの検証（2026-10-10）— 不採用

学習手続きの最適化（時間減衰重み・early stopping・ハイパーパラメータ探索
×2・top3正規化+探索、いずれもnull。上記各セクション参照）が打ち止めと
なったため、新しい特徴量候補として「節間成績」「体重差」を調査した。
実装前に新規性だけを確認する段階を設け、新規性が確認できた候補のみ
実装・検証した。

### v2_recentの特徴量一覧（`ml/src/ml/features/recent.py`、節間成績の判断根拠）

| 列名 | 対象期間 | 集計単位 | 同じ節のレースを含むか |
|---|---|---|---|
| `avg_finish_pos_recent10`/`win_rate_recent10`/`place2_rate_recent10`/`place3_rate_recent10`/`avg_st_recent10` | 直近10走（**race_date<当日のレース数ベース**、日数ではない） | 選手個人、**全場・全節通算**（場・節を問わない） | 含む（場別フィルタなし） |
| `lane_win_rate_recent50`/`lane_avg_start_course_recent50` | 直近50走（同枠番のみ） | 選手個人×当該枠番、全場・全節通算 | 含む |
| `maeduke_rate_recent50` | 直近50走（start_course確定分のみ） | 選手個人、全場・全節通算 | 含む |
| `stadium_win_rate_recent30` | 直近30走（**当該場のみ**） | 選手個人×当該場、**複数節にわたる**通算（節の区切りなし） | 含む（当該場なら節を問わず） |
| `motor_place2_rate_recent20` | 直近20走 | **モーター単位**(stadium_id, motor_no)、選手に依存しない | 含む（当該モーターなら節を問わず） |
| `day_of_meet` | — | 場×連続開催日のまとまり(節)を検出し、その節の何日目かを表す整数。集計値ではない | — |
| `maeduke_count_in_race` | — | 同一レース6艇中、`maeduke_rate_recent50>=0.2`の艇数 | — |

いずれも「節で区切る」フィルタは存在せず、全てrace数ベースの窓（または
`stadium_win_rate_recent30`のように場だけで絞るが節は跨ぐ）である。

### 第1段階①: 節間成績 — 中止（新規性が乏しいと判断）

「節間成績」= その節の初日から前日までの、同じ選手の着順・ST・勝率等。
v2_recentの「直近10走」系の特徴量を上表の通り確認した結果、対象期間は
全てrace数ベースの窓（直近10走・直近30走・直近50走等、日数ではなくレース
数で区切る）で、集計単位は選手個人（全場通算、枠番別に絞る場合もあるが
節の区切りとは無関係）。当該レースと同じ節のレースが含まれるかは、窓の
大きさと節内の消化レース数次第で決まる。

実測(2026-08の1ヶ月、29,592 race_entries)で検証した結果:

| day_of_meet | 節内消化レース数(avg) | うち「直近10走」に含まれる件数(avg) | 重複率 |
|---|---|---|---|
| 1 | 0.00 | 0.00 | — |
| 2 | 1.49 | 1.49 | **100%** |
| 3 | 3.16 | 3.16 | **100%** |
| 4 | 4.73 | 4.73 | **100%** |
| 5 | 6.34 | 6.34 | **100%** |
| 6 | 7.86 | 7.86 | **100%** |
| 7 | 8.94 | 8.94 | **100%** |

day_of_meet 2〜7の**全てで両者が小数点2桁まで完全一致**し、**節内の消化
レースは例外なく100%「直近10走」の窓に含まれていた**（29,592件中、
例外0件）。節4日目では平均4.73走が節内で消化済みで、その4.73走は例外なく
全て「直近10走」の窓の中に既に含まれている。競艇の節は最長でも7日程度・
1日1〜2走のため、節の累積レース数がv2_recentの最も狭い窓(直近10走)を
超えることが観測範囲内で一度も無かったため（`lane_win_rate_recent50`
(直近50走)・`motor_place2_rate_recent20`(直近20走)等のより広い窓なら
言うまでもなく重複する）。

さらに「節だけで平均した値(節間成績)」と「直近10走(他の節のレースを
含みうる希釈済みの値)」の乖離そのものを見ると、**day_of_meet=2(節内
レース数平均1.49件)では乖離average|diff|=0.196・相関0.44**で確かに値は
違うが、これは単に標本数が1〜2件しかないことによる高分散（コイントスに
近い）であり、**day_of_meet=6(節内レース数平均7.78件)では乖離0.041・
相関0.92まで収束**し、既存特徴量とほぼ同じ値になる。つまり**「節だけで
分離した値」が既存特徴量と有意に違う区間(day2-3)は、その値自体が最も
信頼できない（標本数1.5〜3件）区間と完全に重なっており、新規性と信頼性
が両立する「使える区間」が存在しない**。

判断: これは学習手続きの最適化と同種（既存情報の重み付け変更に近い）の
介入であり、本セッションの他の実験(v4_stadium・ハイパーパラメータ探索等、
いずれもnull)のパターンと整合する。「既存特徴量と有意に違う区間」と
「統計的に信頼できる区間」が両立しないため実装しても実益が出る可能性が
低いと判断し、実装・検証に進まず**中止**した。

### 第1段階②: 体重差 — 新規性を確認、実装に進む

v1_basic.weight(番組表発表時点の公表体重)とv5_exhibition.exhibit_weight
(直前計量の実測値)の差分は、grep確認の結果既存のどの特徴量層にも
存在しなかった。LightGBMは2列から差を学習できるが、木は単一特徴量で
分割するため差分を明示した方が分割しやすいという想定で追加対象とした。
adjusted_weight(調整重量)は体重そのものではなく、クラス別規定重量に
満たない場合に追加されるハンデ用おもりの重量であり別概念のため、
差分の対象外とした。

### 第2段階: v6_weight_diffの実装・検証 — 効果なし

`ml/src/ml/features/weight_diff.py`(feature_version='v6_weight_diff')に
`weight_diff_from_program = exhibit_weight - weight` の1特徴量のみを
追加（既存のv1〜v5層は無変更）。リーク防止・source分岐は同じ
race_before_info由来のv5_exhibitionと完全に同じロジックを踏襲した。
全期間(2023-09-01〜2026-10-09、1,038,018行)で生成済み。

**値の分布（2026-10-10詳細確認）**:
- 平均**-0.49kg**、標準偏差**1.04kg**
- 分位点: 最小**-11.1kg** / 25%**-1.0kg** / 中央**-0.4kg** / 75%**+0.1kg**
  / 最大**+4.5kg**
- 値が0の割合: **17.4%**、|値|≤0.5kgの割合: **43.9%**、|値|≤1.0kgの割合:
  **73.2%**
- 欠損率: **0.76%**（v5_exhibitionと同水準）

walk-forward(6fold)でv1+v2+v3+v5とv1+v2+v3+v5+v6を比較
(`ml/src/ml/models/weight_diff_experiment.py`、新規):

- **1着的中率**: 平均差-0.01pt、その場で算出した目安0.06pt以内、
  全6fold中改善2件/悪化4件で**全fold一貫を満たさず**。確立した判定ルールで
  「誤差の範囲/不採用」。
- **confident_top3**(閾値0.70〜0.99を0.01刻みで再スイープ、全6foldプール、
  本番API定義): 実績90%を保ったまま該当数を最大化する閾値はv5/v6とも
  0.96で一致。該当数はv5=48,275件(実績90.23%)に対しv6=48,258件
  (実績90.18%)と、**v6はむしろ17件少なく0.05pt低い**。改善なし。
  **注意**: ここでの0.96という閾値・48,275件等の数値はis_winnerモデル→
  Plackett-Luce展開由来のp_top3（CLAUDE.md「全期間バックフィル完了と
  v5の本番split検証」等、v5 vs v3比較と同じ方式）であり、**本番の
  confident_top3が実際に使うtop3直接学習モデル(閾値0.84、CLAUDE.md
  「本番モデルを2本立て構成に変更」参照)とは全く別の確率スケール**。
  v5とv6のA/B比較としては有効だが、この0.96/48,275件という絶対値を
  本番の実績数値と混同しないこと。
- **feature importance**(fold6のbooster、学習窓が最大): 新規追加した
  `weight_diff_from_program`は**gain基準で全52特徴量中36位**(gain=1318.9)
  に沈み、上位20位以内にも入らなかった。1位`course_predicted`
  (gain=655,767.6)の**約0.2%**に過ぎない。

**分布と結果の関係（重要な訂正）**: 着手前は「分散が小さすぎて効かない」
という仮説を想定していたが、実際の分布を見るとこれは誤りだった。
標準偏差1.04kg、|値|>1.0kgが26.8%、番組表発表後に平均0.49kg体重が減る
という系統的な傾向まで実在しており、**情報自体は存在していた**。
それでもgainが1位の0.2%しかなかったという事実は、**「分布が無かったから
効かなかった」のではなく「分布はあったが、±1kg程度の体重変動はレース
結果に対してほぼ無力か、既存50特徴量(選手の実力・直近成績・展示タイム等)
に既に吸収されている」**ことを意味する。分散の大きさと予測への寄与は
別の問題であり、これを混同しないこと。

### 結論

節間成績・体重差という「既存の生の値を節で切り直す/差分を取る」という
形の特徴量は、第1段階の事前調査(節間成績)・第2段階の実装検証(体重差)の
どちらも新規性・効果を示せなかった。

### 本番モデルの改良施策・通算一覧（2026-10-10時点）

これまでに検証した施策を1箇所にまとめる。今後、同種の最適化に着手する
前にこの一覧を判断材料として参照すること。

| 施策 | 結果 | 詳細 |
|---|---|---|
| v4_stadium（場・枠番の過去統計） | **null** | 的中率-0.01pt、log loss/Brierも悪化。「v4_stadium 特徴量」参照 |
| v5_exhibition（展示タイム・course_predicted・気象等） | **採用（+0.42pt、有意）** | 的中率56.20%→56.69%、confident_top3も+687件・+0.13pt。全特徴量中最大の改善。「全期間バックフィル完了とv5の本番split検証」参照 |
| top3直接学習モデル(is_top3) | **採用** | confident_top3の閾値0.84で該当数を維持したまま、確率のキャリブレーションが大幅改善(±13pt→±1.14pt)。「本番モデルを2本立て構成に変更」参照 |
| 時間減衰サンプル重み(半減期6/12/18/24ヶ月) | **null** | 全候補で平均差ほぼゼロ(+0.01〜0.02pt)、全fold一貫も満たさず。「walk-forward検証の導入とv5の有意性判定」参照 |
| early stopping(木の本数の動的決定) | **null** | best_iterationが100〜280と2.8倍ばらつくが性能はほぼ変わらず。同上参照 |
| ハイパーパラメータ探索(winnerモデル、40試行) | **null** | 上位5件が0.059pt以内に団子、ベースラインが実質3位相当。同上参照 |
| top3正規化方式(線形+clip/clipなし/ロジット平行移動) | **null** | 3方式で該当数・実績ほぼ差なし、clipは実質無害と判明。「top3モデルの正規化とハイパーパラメータ探索」参照 |
| ハイパーパラメータ探索(top3モデル、20試行) | **null** | ベースラインが候補群の中で3位相当、全fold一貫は探索の事後選択バイアスで説明可能(約27%)。同上参照 |
| v6_weight_diff(体重差) | **null** | 的中率平均差-0.01pt(全fold不一致)、confident_top3は該当数・実績とも僅かに悪化、feature importance 36/52位。本節参照 |
| 節間成績 | **null(新規性なしで実装前に中止)** | v2_recentとの重複率100%(29,592件、例外0件)。本節参照 |

**合計10施策中、採用2件（いずれも「締切前には取得できない別のデータ
ソース」由来、v5_exhibition・top3直接学習）、null 8件（いずれも既存の
生データを重み付け・集計単位・正規化方式・単純な差分等で再加工した
施策）**。

**結論**: 残る候補はいずれも手元に無いデータを要するか、測定済みで
否定されている。モデルの改良はここで一旦完成とし、実運用データの蓄積に
移る。既存の生データ列を再加工しただけの特徴量・学習手続きの変更では
伸びしろが無いことが10施策を通じて一貫して示された。新しい伸びしろは、
これまで一切使っていない別のデータソース（オッズ統合等、CLAUDE.md
「オッズ(odds_snapshots)を特徴量として使えるか」参照）からしか得られない
可能性が高い。
実装(`weight_diff.py`/`weight_diff_experiment.py`/
`lgbm.py`の`fetch_v6_dataset`等)は削除せず残してある。

## 運用フェーズへの移行とdata:reportコマンドの新設（2026-10-10）

モデルの改良を一旦完成とし（上記「本番モデルの改良施策・通算一覧」参照）、
実運用データの蓄積フェーズに移った。`data:catch-up:status`はOK/NGの
判定だけで、stage2・オッズは意図的に判定外（部分的なカバレッジが正常の
ため）にしており、運用の実態が数字で見えない。毎回SQLを書くのは
非現実的なため、`php artisan data:report --days=N`を新設した
(`app/Console/Commands/DataReport.php`)。**判定は一切行わず、数字を
並べるだけ**（異常かどうかは人が見て判断する）。

出力は3つの表（いずれも日別＋期間合計）:
1. **カバレッジ**: races/results/payouts/predictions/top3_predictions/
   stage2/live_beforeinfo(race_before_info.source='live')/
   odds_any(odds_snapshotsの存在)/
   odds_before_cutoff(captured_at <= deadline_at-10分を満たすオッズの
   存在、T-13分への変更が効いているかの確認用）の件数と比率。
   results/payoutsは中止レース除外後の件数が分母、それ以外はraces総数が
   分母（`DataCoverage::refreshCoverage()`と同じ分母の使い分け）。
2. **failed_jobs**: ジョブ種別(displayName)ごとの件数（CaptureOddsJobの
   中止レース起因の失敗は2026-10-10の修正でfailed_jobsに残らなくなった
   ため、ここに出るものは本当の失敗。CLAUDE.md「CaptureOddsJobの失敗
   88件の調査」参照）。
3. **prediction_judgments**: stage別(1=v3当日朝/2=v5直前再予測)の判定済み
   件数・的中数・的中率。

動作確認（2026-10-10、`--days=10`/`--days=30`で実行）: 0.26秒で完了。
stage2/live_beforeinfoは2026-10-01〜08が0%、2026-10-09から74.3%・
2026-10-10が50.0%（まだ当日進行中）と、CLAUDE.md「stage2初回稼働の実態」
の記録と整合。odds_before_cutoffは2026-10-10時点でもまだ全日0件
（T-13分への変更(`adff587`)は当日朝の`odds:schedule-today`実行時点の
コードで予約が決まるため、コミットが当日の06:05 JST実行枠より後だと
反映が翌日以降になる。これは想定通りの遅延であり不具合ではない）。

### 3ヶ月後(2027-01頃)に評価すること

実運用データが貯まった時点で、以下を評価する。評価の際はwalk-forwardで
確立した判定ルール（**実験ごとに目安をその場で算出し、全fold一貫を要求**。
探索（複数候補から事後選択する場面）の場合は**ベースラインが候補群の中で
何位に位置するかの確認も併用**する。CLAUDE.md「walk-forward検証の導入と
v5の有意性判定」「top3モデルの正規化とハイパーパラメータ探索」参照）を
踏襲すること。

1. **stage1(v3) vs stage2(v5)の本番A/B**: `predictions:judge`がstage1/
   stage2を独立に判定するため、stage2が存在するレースでは**同一レース上
   の直接対決**になる(`data:report`の`prediction_judgments`表がそのまま
   素データ)。バックテストの+0.42pt(CLAUDE.md「v5がv3を有意に上回ると
   判定」参照)が実運用で再現するかを、ペア比較で検証する。**これは過去
   データのwalk-forward検証では得られない種類の証拠**（本番の配信経路・
   実際のキュー処理遅延・実際のPC電源off運用を全て経た上での実測）である
   点を踏まえて評価すること。
2. **confident_top3の実績が90%を超えるか**: 現在画面に「84%以上、実績
   90.4%」と表示している(`RacesToday.vue`)。実運用の`prediction_judgments`
   ベースでの実績を確認し、表示と整合するかを検証する。
3. **stage2の実カバレッジ**: 初日(2026-10-09)は107/144=74.3%。
   mb_scrub修正(`828d2ab`)後の実測値を`data:report`で確認する。PC停止
   時刻以降のナイターレースは構造的に取れないため、上限は100%にならない
   見込み（CLAUDE.md「stage2初回稼働の実態・バッチ監視の欠陥・2件の実バグ
   修正」参照）。
4. **オッズの再検証**: T-13分への変更(`adff587`)後のデータが3ヶ月で約
   13,000レース分貯まる見込み。2026-10-08の分析は1,843レースという弱い
   標本だったため、そこで初めてまともな再検証ができる。市場の暗黙確率と
   モデルの比較を、リーク条件(captured_at <= cutoff_at)を満たしたデータで
   やり直す。

## v4_stadium 特徴量（2026-09-21）
- 場の特性・選手の場適性を追加。`ml/src/ml/features/stadium.py`。
  1. 場×枠番の基礎統計（race_dateより厳密に前の全履歴、expanding window。
     全期間の集計値を使うとリークになるため、PostgreSQLのウィンドウ関数
     `RANGE BETWEEN UNBOUNDED PRECEDING AND '1 day' PRECEDING`で
     1パス計算している）:
     `stadium_lane_win_rate` / `stadium_lane_avg_start_course` /
     `stadium_maeduke_rate`（場全体・枠番問わずの前付け発生率）
  2. 選手の場適性（直近30走、v2_recentと同じLATERAL/LIMIT方式）:
     `racer_stadium_lane_win_rate_recent30` /
     `racer_stadium_avg_start_course_recent30`
  3. 気象情報は**実装しなかった**。Kファイルのレースヘッダには天候・
     風向・風速・波高が実際に含まれている（例:
     `H1800m  晴　  風  北西　 2m  波　  1cm`）が、Kファイルは結果ファイル
     であり、この値はレース施行時点(結果確定後)のものであって締切10分前
     には存在しない。特徴量に使うとリークになるため見送った
     （締切前に取得できる気象ソースとしてはbeforeinfoページのものがあるが、
     現在は財団への確認待ちで停止中。上記参照）。
- 1ヶ月分(2026-08)で計測: 6.3秒（うち準備フェーズ=全履歴のwindow計算が
  3.7秒でほぼ固定費用、対象期間分の読み取り+書き込みが2.6秒）。
  全期間(2023-09-01〜2026-09-21、1,026,216行)は59.8秒で完了。
- **v1+v2+v3 と v1+v2+v3+v4 の比較（検証期間、binary、同一パラメータ）**:
  的中率 56.20%→56.19%(-0.01pt)、log loss 1.1974→1.1980(+0.0005)、
  Brier 0.5834→0.5836(+0.0002)。**改善なし（誤差の範囲でわずかに悪化）**。
  feature importanceでは`stadium_lane_win_rate`が5位(gain=38,411)に入り
  モデル自体は使っているが、既存のv2`lane_win_rate_recent50`（選手個人の
  枠番別1着率）と情報が重複しており、held-out性能の向上には寄与しな
  かったと考えられる。v4は現状predictions生成には使わず、featuresテーブル
  への記録のみに留める。

## ハイパーパラメータチューニング（2026-09-21）
- `ml/src/ml/models/tune.py`（Optuna、`uv add optuna`で追加）。
  検証期間(2026-01-01〜09-17)を汚染しないため、学習期間自体を
  さらに時系列で2分割してチューニングした:
  - チューニング用学習: 2023-09-01〜2025-06-30 (613,368行)
  - チューニング用検証: 2025-07-01〜2025-12-31 (164,736行)
  - 検証期間はチューニング中一切参照していない
- 探索対象: learning_rate, num_leaves, min_child_samples,
  feature_fraction, bagging_fraction, lambda_l2（TPESampler, seed=0,
  40試行）。目的関数はLightGBM自身のbinary_loglossではなく、
  lgbm.evaluate()のレース単位log loss（プロダクト方針の主要指標と同じ
  定義）。
- ベストパラメータ（チューニング用検証でのlog loss=1.21959）:
  learning_rate=0.0374, num_leaves=125, min_child_samples=141,
  feature_fraction=0.715, bagging_fraction=0.901, lambda_l2=0.00036
- 上記で学習期間全体(2023-09-01〜2025-12-31)を再学習し、
  `v3_binary_tuned_20260921`として保存。検証期間で**一度だけ**評価
  （このあと結果を見てパラメータを調整し直すことはしていない）:
  | | 的中率 | log loss | Brier |
  |---|---|---|---|
  | 現行(v3_binary_20260920) | 56.20% | 1.19744 | 0.58339 |
  | チューニング後(v3_binary_tuned_20260921) | 56.20% | 1.19659 | 0.58302 |
  | 差分 | -0.00pt | -0.00085 | -0.00037 |
  的中率は同水準のまま、log loss・Brierともにわずかに改善。motor_win_rate_2
  修正の時と同程度の小さな改善幅で、劇的な効果はないが一貫して同方向。
  **本番モデルの切替はまだ行っていない**（ユーザー判断待ち）。
