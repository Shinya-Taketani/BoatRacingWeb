<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Database\Schema\Blueprint;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Schema;

return new class extends Migration
{
    public function up(): void
    {
        // 公式サイト(boatrace.jp)の直前情報(beforeinfo)ページから取得する
        // 艇ごとの情報。締切のT-14〜16分頃に公開されることを実測済みで、
        // T-12にジョブを予約して取得する（CaptureBeforeInfoJob）。
        //
        // 締切を過ぎると二度と取得できない一過性の情報のため、odds_snapshots
        // と同様に取得できたその時点の値を記録するだけで、後から埋め直す
        // ことはできない。当面は記録のみで学習には使わない
        // （2026-09-21時点。数ヶ月分貯まってから v4_exhibition として
        // 特徴量に追加する想定）。
        Schema::create('race_before_info', function (Blueprint $table) {
            $table->id();
            $table->foreignId('race_id')->constrained('races')->cascadeOnDelete();
            $table->unsignedTinyInteger('lane'); // 枠番 1-6
            $table->decimal('weight', 5, 2)->nullable(); // 当日体重
            $table->decimal('adjusted_weight', 5, 2)->nullable(); // 調整重量
            $table->decimal('exhibit_time', 4, 2)->nullable(); // 展示タイム
            $table->decimal('tilt', 3, 1)->nullable(); // チルト角（マイナスあり）
            $table->boolean('propeller_changed')->default(false); // プロペラ交換の有無
            $table->string('parts_exchanged')->nullable(); // 交換部品（カンマ区切り、例: "キャブ"）
            // スタート展示（展示航走時のスタート）でどのコースに入ったか。
            // 「並び」欄の艇番とコース(行番号)の対応から確定する値なので、
            // 実際のレースの進入コースそのものではなく、あくまで展示時点の予想値。
            $table->unsignedTinyInteger('course_predicted')->nullable();
            $table->decimal('st_exhibit', 4, 2)->nullable(); // スタート展示のST
            $table->timestampTz('captured_at'); // 取得時刻
            $table->timestamps();

            $table->unique(['race_id', 'lane']);
        });

        DB::statement(
            'ALTER TABLE race_before_info ADD CONSTRAINT race_before_info_lane_range CHECK (lane BETWEEN 1 AND 6)'
        );
        DB::statement(
            'ALTER TABLE race_before_info ADD CONSTRAINT race_before_info_course_predicted_range '
            .'CHECK (course_predicted IS NULL OR course_predicted BETWEEN 1 AND 6)'
        );
    }

    public function down(): void
    {
        Schema::dropIfExists('race_before_info');
    }
};
