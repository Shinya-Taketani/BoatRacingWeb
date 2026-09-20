<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Database\Schema\Blueprint;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Schema;

return new class extends Migration
{
    public function up(): void
    {
        Schema::create('races', function (Blueprint $table) {
            $table->id();
            $table->date('race_date');
            $table->foreignId('stadium_id')->constrained('stadiums')->restrictOnDelete();
            $table->unsignedTinyInteger('race_no'); // 1-12
            $table->timestampTz('deadline_at'); // 締切時刻
            $table->unsignedSmallInteger('distance_m')->nullable();
            $table->string('title')->nullable(); // レース名/グレード
            $table->timestamps();

            $table->unique(['race_date', 'stadium_id', 'race_no']);
        });

        DB::statement(
            'ALTER TABLE races ADD CONSTRAINT races_race_no_range CHECK (race_no BETWEEN 1 AND 12)'
        );
    }

    public function down(): void
    {
        Schema::dropIfExists('races');
    }
};
