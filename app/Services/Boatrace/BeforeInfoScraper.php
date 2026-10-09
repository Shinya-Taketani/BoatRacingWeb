<?php

namespace App\Services\Boatrace;

use Carbon\CarbonImmutable;
use DateTimeInterface;
use Illuminate\Support\Facades\Http;
use Symfony\Component\DomCrawler\Crawler;

/**
 * BOAT RACE公式サイトの直前情報ページ(beforeinfo)を取得・パースする。
 *
 * URL: https://www.boatrace.jp/owpc/pc/race/beforeinfo?rno={race_no}&jcd={stadium_code:02d}&hd={Ymd}
 *
 * 実測(2026-09-21)では締切のT-14〜16分頃に展示タイム・スタート展示が
 * 公開される。締切を過ぎると二度と取得できないため、CaptureBeforeInfoJob
 * が締切T-12にこのスクレイパーを呼ぶ。
 *
 * 艇ごとの情報テーブル(table.is-w748)は6艇分のtbodyが並び、各tbodyは
 * rowspanを使った4行(前走成績欄含む)で構成される。フラットに列挙すると
 * 17個のtdが並ぶ固定レイアウトで、位置ベースで読み取る
 * （TrifectaOddsScraperのrowspan追跡と同じ考え方）。
 *
 * スタート展示テーブル(table.is-w238)は「コース」ごとに1行(=6行)あり、
 * 各行の並び(艇画像)に表示されている艇番がそのコースに入った艇、という
 * 対応になっている。展示時点の進入予想はここから確定する
 * （実際のレースの進入コースではない点に注意）。
 */
class BeforeInfoScraper
{
    private const BASE_URL = 'https://www.boatrace.jp/owpc/pc/race/beforeinfo';

    private const USER_AGENT = 'Mozilla/5.0 (compatible; BoatRacingWeb beforeinfo collector)';

    public function fetch(int $stadiumCode, int $raceNo, DateTimeInterface $raceDate): BeforeInfoResult
    {
        $response = Http::withHeaders(['User-Agent' => self::USER_AGENT])
            ->timeout(15)
            ->get(self::BASE_URL, [
                'rno' => $raceNo,
                'jcd' => sprintf('%02d', $stadiumCode),
                'hd' => $raceDate->format('Ymd'),
            ]);

        $capturedAt = CarbonImmutable::now();

        if (! $response->successful()) {
            throw new BeforeInfoFetchException(
                "beforeinfo request failed: HTTP {$response->status()} ".
                "(jcd={$stadiumCode}, rno={$raceNo}, hd={$raceDate->format('Ymd')})"
            );
        }

        $html = $response->body();
        // レスポンス本文はContent-Type/meta共にUTF-8を宣言しているが、
        // 不正なバイト列(例: マルチバイト文字の途中で途切れた断片)が混入し、
        // そのままPostgreSQLへinsertするとエンコーディングエラーで該当レース
        // 全体の取り込みが失敗する（2026-10-09発覚）。
        // 【2026-10-10訂正】導入時「発生頻度・再現性ともに低い」と記録したが
        // 誤りだった。failed_jobsをApp\Jobs\CaptureBeforeInfoJobに限定して
        // 実数を確認したところ、ライブ取得が実際に稼働した唯一の日
        // (2026-10-09、144レース中33レースが失敗、全件がこのエンコーディング
        // エラー)で失敗率は33/144≈22.9%と、「稀」とは言えない頻度だった
        // （当初「2026-09-21の稼働開始以降75レースで発生」と記録したのも誤りで、
        // 実際はCaptureOddsJob(無関係な別ジョブ)の失敗88件を誤って合算していた
        // ため。App\Jobs\CaptureBeforeInfoJobだけに絞ると33件であり、かつ
        // beforeinfo:schedule-todayの再有効化コミット(2026-10-08 07:54 JST)は
        // その日の06:05の実行枠を過ぎてから入ったため、実際に初めて動いたのは
        // 翌日2026-10-09が最初。「2026-09-21以降」という前提自体が誤りだった）。
        // 原因はCDN側の中間変換等が疑われるが未特定のまま。ただし22.9%という
        // 頻度でも、mb_scrub()で不正なバイト列だけを置換文字に置き換え残りの
        // 正常な部分を保持するという対処の妥当性自体は変わらない
        // （個別調査より、どのような不正バイト列が来ても安全に失う防御の方が
        // 実効的なため）。
        $html = mb_scrub($html, 'UTF-8');

        if (str_contains($html, 'データがありません')) {
            throw new BeforeInfoFetchException(
                "no beforeinfo data for jcd={$stadiumCode}, rno={$raceNo}, hd={$raceDate->format('Ymd')} ".
                '(race may not exist)'
            );
        }

        $crawler = new Crawler;
        $crawler->addHtmlContent($html, 'UTF-8');

        $boats = $this->parseBoats($crawler, $stadiumCode, $raceNo);
        $this->mergeExhibitStart($crawler, $boats);
        $weather = $this->parseWeather($crawler, $raceDate);

        return new BeforeInfoResult($capturedAt, $boats, $weather);
    }

    /**
     * @return array<int, array<string, mixed>> laneをキーにした配列
     */
    private function parseBoats(Crawler $crawler, int $stadiumCode, int $raceNo): array
    {
        $tables = $crawler->filter('table.is-w748');
        if ($tables->count() === 0) {
            throw new BeforeInfoFetchException(
                "艇情報テーブルが見つかりません (jcd={$stadiumCode}, rno={$raceNo})"
            );
        }

        $boats = [];
        $tables->first()->filter('tbody')->each(function (Crawler $tbody) use (&$boats) {
            $tds = [];
            $tbody->filter('td')->each(function (Crawler $td) use (&$tds) {
                $tds[] = $td;
            });

            // 固定レイアウト: 0=枠 1=写真 2=名前 3=体重 4=展示タイム 5=チルト
            // 6=プロペラ 7=部品交換 8="R" 9=空 10="進入" 11=空
            // 12=調整重量 13="ST" 14=空 15="着順" 16=空
            $lane = (int) trim($tds[0]->text());

            $boats[$lane] = [
                'lane' => $lane,
                'weight' => $this->numeric($tds[3]->text('')),
                'adjusted_weight' => $this->numeric($tds[12]->text('')),
                'exhibit_time' => $this->numeric($tds[4]->text('')),
                'tilt' => $this->numeric($tds[5]->text('')),
                'propeller_changed' => trim($tds[6]->text('')) === '新',
                'parts_exchanged' => $this->parseParts($tds[7]),
                'course_predicted' => null,
                'st_exhibit' => null,
            ];
        });

        return $boats;
    }

    private function parseParts(Crawler $partsCell): ?string
    {
        $labels = [];
        $partsCell->filter('li')->each(function (Crawler $li) use (&$labels) {
            $text = trim($li->text(''), " \t\n\r\0\x0B\u{3000}");
            if ($text !== '') {
                $labels[] = $text;
            }
        });

        return $labels === [] ? null : implode(',', $labels);
    }

    /**
     * @param  array<int, array<string, mixed>>  $boats  参照で書き換える
     */
    private function mergeExhibitStart(Crawler $crawler, array &$boats): void
    {
        $table = $crawler->filter('table.is-w238');
        if ($table->count() === 0) {
            return;
        }

        $rows = $table->first()->filter('tbody tr');
        $rows->each(function (Crawler $row, int $index) use (&$boats) {
            $course = $index + 1; // 行番号=コース(1-6)

            $numberSpan = $row->filter('.table1_boatImage1Number');
            if ($numberSpan->count() === 0) {
                return; // 未公開（空行）
            }

            $lane = (int) trim($numberSpan->text());
            $timeSpan = $row->filter('.table1_boatImage1Time');
            $st = $timeSpan->count() > 0 ? $this->parseStTime($timeSpan->text('')) : null;

            if (isset($boats[$lane])) {
                $boats[$lane]['course_predicted'] = $course;
                $boats[$lane]['st_exhibit'] = $st;
            }
        });
    }

    /**
     * @return array{
     *     temperature: float|null,
     *     weather_condition: string|null,
     *     wind_speed: float|null,
     *     wind_direction_code: int|null,
     *     water_temperature: float|null,
     *     wave_height: float|null,
     *     measured_at: CarbonImmutable|null,
     * }
     */
    private function parseWeather(Crawler $crawler, DateTimeInterface $raceDate): array
    {
        $weatherDiv = $crawler->filter('div.weather1');
        $empty = [
            'temperature' => null,
            'weather_condition' => null,
            'wind_speed' => null,
            'wind_direction_code' => null,
            'water_temperature' => null,
            'wave_height' => null,
            'measured_at' => null,
        ];

        if ($weatherDiv->count() === 0) {
            return $empty;
        }

        $title = trim($weatherDiv->first()->filter('.weather1_title')->text(''));
        $measuredAt = null;
        if (preg_match('/(\d{1,2}):(\d{2})現在/u', $title, $m)) {
            // Asia/Tokyoで組み立てた直後にUTCへ変換しておく。DBのpgsql接続は
            // 'timezone' => 'UTC' でDBセッションをapp.timezone(UTC)に揃えて
            // あるため、Eloquentはnaive文字列をUTCとして書き込む。Carbon側の
            // タイムゾーンをAsia/Tokyoのままbindすると、format()がAsia/Tokyoの
            // 時刻表記をそのまま出力しUTCとして誤解釈される(9時間ズレる)ため、
            // 明示的にutc()してから返す（CaptureOddsJobのcaptured_at検証で
            // 見つかった既知のバグと同じ原因）。
            $measuredAt = CarbonImmutable::parse(
                $raceDate->format('Y-m-d')." {$m[1]}:{$m[2]}:00",
                'Asia/Tokyo'
            )->utc();
        }

        $labelData = fn (string $selector) => $this->numeric(
            $weatherDiv->first()->filter("{$selector} .weather1_bodyUnitLabelData")->count() > 0
                ? $weatherDiv->first()->filter("{$selector} .weather1_bodyUnitLabelData")->text('')
                : null
        );

        $conditionNode = $weatherDiv->first()->filter('.is-weather .weather1_bodyUnitLabelTitle');
        $condition = $conditionNode->count() > 0 ? trim($conditionNode->text('')) : null;

        $windDirectionCode = null;
        $windImg = $weatherDiv->first()->filter('.is-windDirection .weather1_bodyUnitImage');
        if ($windImg->count() > 0) {
            $class = $windImg->attr('class') ?? '';
            if (preg_match('/is-wind(\d+)/', $class, $m)) {
                $windDirectionCode = (int) $m[1];
            }
        }

        return [
            'temperature' => $labelData('.is-direction'),
            'weather_condition' => $condition === '' ? null : $condition,
            'wind_speed' => $labelData('.is-wind'),
            'wind_direction_code' => $windDirectionCode,
            'water_temperature' => $labelData('.is-waterTemperature'),
            'wave_height' => $labelData('.is-wave'),
            'measured_at' => $measuredAt,
        ];
    }

    private function numeric(?string $text): ?float
    {
        if ($text === null) {
            return null;
        }

        $trimmed = trim(str_replace("\u{00A0}", '', $text));
        if ($trimmed === '' || $trimmed === '&nbsp;') {
            return null;
        }

        if (! preg_match('/-?[\d.]+/', $trimmed, $m)) {
            return null;
        }

        return (float) $m[0];
    }

    /**
     * スタート展示のST表記は".04"のように整数部が省略されているため、
     * numeric()にそのまま渡すと0.04ではなく4になってしまう。先頭が"."の
     * 場合は"0"を補ってから数値化する。
     */
    private function parseStTime(string $text): ?float
    {
        $trimmed = trim($text);
        if ($trimmed === '') {
            return null;
        }
        if (str_starts_with($trimmed, '.')) {
            $trimmed = '0'.$trimmed;
        } elseif (str_starts_with($trimmed, '-.')) {
            $trimmed = '-0'.substr($trimmed, 1);
        }

        return $this->numeric($trimmed);
    }
}
