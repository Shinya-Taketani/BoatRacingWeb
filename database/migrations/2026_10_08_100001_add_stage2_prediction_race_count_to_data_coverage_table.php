<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Database\Schema\Blueprint;
use Illuminate\Support\Facades\Schema;

return new class extends Migration
{
    public function up(): void
    {
        // stage2(直前再予測、v5_exhibitionを含む)の生成状況を記録する整数カウント。
        // odds_race_countと同じ扱い（boolean化しない）: PC電源off運用のため
        // 1日の途中で停止することがあり、カバレッジが部分的になるのが正常。
        // booleanフラグにすると「レース数と一致して初めてtrue」の定義上、
        // data:catch-up:statusが常にNGになってしまう（criticalGapsFor()には
        // 含めず、data:catch-upのレポート出力にのみ出す。CLAUDE.md「stage2構成」
        // 参照）。
        Schema::table('data_coverage', function (Blueprint $table) {
            $table->integer('stage2_prediction_race_count')->default(0)->after('odds_race_count');
        });
    }

    public function down(): void
    {
        Schema::table('data_coverage', function (Blueprint $table) {
            $table->dropColumn('stage2_prediction_race_count');
        });
    }
};
