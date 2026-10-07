<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Database\Schema\Blueprint;
use Illuminate\Support\Facades\Schema;

return new class extends Migration
{
    public function up(): void
    {
        // 2026-10-04、1着予測モデルと3着以内予測モデルの2本立てに変更した
        // ことで、has_predictions(winnerモデル)だけ見ていると、
        // top3モデル側だけ欠けているケースを検知できない
        // （CLAUDE.md「本番モデルを2本立て構成に変更」参照）。
        // has_predictions/has_top3_predictionsを別カラムに分離し、
        // どちらが欠けているかを区別できるようにする。
        Schema::table('data_coverage', function (Blueprint $table) {
            $table->boolean('has_top3_predictions')->default(false)->after('has_predictions');
        });
    }

    public function down(): void
    {
        Schema::table('data_coverage', function (Blueprint $table) {
            $table->dropColumn('has_top3_predictions');
        });
    }
};
