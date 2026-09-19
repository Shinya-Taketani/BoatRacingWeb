<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Database\Schema\Blueprint;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Schema;

return new class extends Migration
{
    public function up(): void
    {
        // racer_id + 期間の重複禁止用 EXCLUDE 制約に必要
        DB::statement('CREATE EXTENSION IF NOT EXISTS btree_gist');

        Schema::create('racer_periods', function (Blueprint $table) {
            $table->id();
            $table->foreignId('racer_id')->constrained('racers')->restrictOnDelete();
            $table->timestampTz('valid_from');
            $table->timestampTz('valid_to')->nullable(); // null = 現行有効
            $table->string('racer_class'); // A1 / A2 / B1 / B2
            $table->string('branch')->nullable(); // 支部
            $table->string('birthplace')->nullable(); // 出身地
            $table->decimal('national_win_rate', 5, 2)->nullable();
            $table->decimal('national_win_rate_2', 5, 2)->nullable(); // 全国2連対率
            $table->decimal('local_win_rate', 5, 2)->nullable();
            $table->decimal('local_win_rate_2', 5, 2)->nullable(); // 当地2連対率
            $table->decimal('weight', 5, 2)->nullable();
            $table->timestamps();

            $table->unique(['racer_id', 'valid_from']);
            $table->index(['racer_id', 'valid_to']);
        });

        DB::statement(
            'ALTER TABLE racer_periods ADD CONSTRAINT racer_periods_valid_to_after_valid_from '
            .'CHECK (valid_to IS NULL OR valid_to > valid_from)'
        );

        // 同一選手の期間が重複することをDBレベルで禁止し、配信時刻に対して
        // 有効なスナップショットが常に一意に定まることを保証する。
        DB::statement(
            'ALTER TABLE racer_periods ADD CONSTRAINT racer_periods_no_overlap '
            .'EXCLUDE USING gist (racer_id WITH =, tstzrange(valid_from, valid_to) WITH &&)'
        );
    }

    public function down(): void
    {
        Schema::dropIfExists('racer_periods');
    }
};
