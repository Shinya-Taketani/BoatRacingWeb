<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Database\Schema\Blueprint;
use Illuminate\Support\Facades\Schema;

return new class extends Migration
{
    public function up(): void
    {
        Schema::create('racers', function (Blueprint $table) {
            $table->id();
            $table->unsignedInteger('registration_number')->unique(); // 全国選手登録番号
            $table->string('name');
            $table->string('name_kana')->nullable();
            $table->date('birth_date')->nullable();
            $table->timestamps();
        });
    }

    public function down(): void
    {
        Schema::dropIfExists('racers');
    }
};
