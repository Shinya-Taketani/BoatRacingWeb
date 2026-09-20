<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Database\Schema\Blueprint;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Schema;

return new class extends Migration
{
    public function up(): void
    {
        // prediction_results は ticket_id が NOT NULL・主キーで prediction_tickets
        // への必須FKであり、「舟券(買い目)ごとの的中判定」専用のテーブルである。
        // 買い目がまだ未実装の現時点では、そこに「予測単位（=最も確率の高い艇が
        // 実際に1着だったか）」の的中を記録できないため、別テーブルとして新設する。
        // prediction_results は将来の買い目実装時のために変更せず残す。
        Schema::create('prediction_judgments', function (Blueprint $table) {
            $table->foreignId('prediction_id')->primary()->constrained('predictions')->cascadeOnDelete();
            $table->unsignedTinyInteger('predicted_lane'); // p_first最大のlane
            $table->unsignedTinyInteger('actual_lane')->nullable(); // 実際の1着lane（全艇失格等でnullもあり得る）
            $table->boolean('hit');
            $table->timestampTz('judged_at');
        });

        DB::statement(
            'ALTER TABLE prediction_judgments ADD CONSTRAINT prediction_judgments_predicted_lane_range '
            .'CHECK (predicted_lane BETWEEN 1 AND 6)'
        );
        DB::statement(
            'ALTER TABLE prediction_judgments ADD CONSTRAINT prediction_judgments_actual_lane_range '
            .'CHECK (actual_lane IS NULL OR actual_lane BETWEEN 1 AND 6)'
        );
    }

    public function down(): void
    {
        Schema::dropIfExists('prediction_judgments');
    }
};
