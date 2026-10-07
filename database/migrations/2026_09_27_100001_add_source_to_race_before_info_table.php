<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Database\Schema\Blueprint;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Schema;

return new class extends Migration
{
    public function up(): void
    {
        // captured_at は「取得した時刻」であって「サイトが公開した時刻」ではない。
        // ライブ取得(締切T-12分の予約ジョブ)ではこの2つはほぼ一致するが、
        // バックフィル(過去分を後日まとめて取得)では captured_at が取得作業を
        // 行った日時になり、対象レースの締切よりずっと後になる
        // （2026-09-27、v5_exhibition特徴量のリーク検証で発覚）。
        // captured_at だけでは両者を区別できないため、取得経路を明示する
        // source 列を追加する。既存の全行(88,632行、2026-06-21〜09-20分)は
        // 過去分の後日取得なので 'backfill' として埋める。
        Schema::table('race_before_info', function (Blueprint $table) {
            $table->string('source')->default('backfill')->after('captured_at');
        });

        DB::statement(
            "ALTER TABLE race_before_info ADD CONSTRAINT race_before_info_source_check ".
            "CHECK (source IN ('live', 'backfill'))"
        );
    }

    public function down(): void
    {
        DB::statement('ALTER TABLE race_before_info DROP CONSTRAINT IF EXISTS race_before_info_source_check');
        Schema::table('race_before_info', function (Blueprint $table) {
            $table->dropColumn('source');
        });
    }
};
