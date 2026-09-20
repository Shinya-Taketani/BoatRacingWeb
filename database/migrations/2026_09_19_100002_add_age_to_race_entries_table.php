<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Database\Schema\Blueprint;
use Illuminate\Support\Facades\Schema;

return new class extends Migration
{
    public function up(): void
    {
        // 番組表(B)には選手の年齢が含まれるが、これまでパース時点で捨てられ
        // どのテーブルにも保存されていなかった（racers.birth_dateも未収集）。
        // 特徴量生成で使うため race_entries に追加する。
        Schema::table('race_entries', function (Blueprint $table) {
            $table->unsignedTinyInteger('age')->nullable()->after('racer_period_id');
        });
    }

    public function down(): void
    {
        Schema::table('race_entries', function (Blueprint $table) {
            $table->dropColumn('age');
        });
    }
};
