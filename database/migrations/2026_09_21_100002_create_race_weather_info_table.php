<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Database\Schema\Blueprint;
use Illuminate\Support\Facades\Schema;

return new class extends Migration
{
    public function up(): void
    {
        // 直前情報ページの水面気象情報（レース単位、艇ごとではないため
        // race_before_infoとは別テーブルにする）。
        Schema::create('race_weather_info', function (Blueprint $table) {
            $table->foreignId('race_id')->primary()->constrained('races')->cascadeOnDelete();
            $table->decimal('temperature', 4, 1)->nullable(); // 気温(℃)
            $table->string('weather_condition')->nullable(); // 天候（晴/曇/雨等、サイト表記そのまま）
            $table->decimal('wind_speed', 4, 1)->nullable(); // 風速(m)
            // 風向はCSSクラス名(is-windN)にアイコンとしてのみ表現されており、
            // Nと実際の方角(北/南東 等)の対応が未確定のため、方角に変換せず
            // 生のコードのまま保持する（2026-09-21時点）。
            $table->unsignedTinyInteger('wind_direction_code')->nullable();
            $table->decimal('water_temperature', 4, 1)->nullable(); // 水温(℃)
            $table->decimal('wave_height', 4, 1)->nullable(); // 波高(cm)
            $table->timestampTz('measured_at')->nullable(); // サイト表記の「HH:MM現在」
            $table->timestampTz('captured_at'); // 取得時刻
            $table->timestamps();
        });
    }

    public function down(): void
    {
        Schema::dropIfExists('race_weather_info');
    }
};
