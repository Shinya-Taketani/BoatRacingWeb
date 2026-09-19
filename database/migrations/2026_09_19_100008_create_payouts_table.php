<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Database\Schema\Blueprint;
use Illuminate\Support\Facades\Schema;

return new class extends Migration
{
    public function up(): void
    {
        // 競走成績(K)ファイルのレースごとの払戻金明細（単勝/複勝/2連単/2連複/
        // 拡連複/3連単/3連複）。これまでresult.pyのパーサーがこの区間を
        // 「自由形式」として意図的に読み飛ばしていたため、確定払戻金の
        // データが一切存在しなかった（回収率計算に必須のため今回追加する）。
        Schema::create('payouts', function (Blueprint $table) {
            $table->id();
            $table->foreignId('race_id')->constrained('races')->cascadeOnDelete();
            $table->string('bet_type'); // 単勝/複勝/2連単/2連複/拡連複/3連単/3連複
            $table->string('combination'); // 例: "3-5-2"（単勝/複勝は艇番単体 "3"）
            $table->unsignedInteger('payout'); // 100円購入あたりの払戻金（円）
            $table->unsignedSmallInteger('popularity_rank')->nullable(); // 人気順（単勝/複勝には無い）
            $table->timestamps();

            $table->unique(['race_id', 'bet_type', 'combination']);
        });
    }

    public function down(): void
    {
        Schema::dropIfExists('payouts');
    }
};
