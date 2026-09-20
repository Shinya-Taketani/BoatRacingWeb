<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Database\Schema\Blueprint;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Schema;

return new class extends Migration
{
    public function up(): void
    {
        Schema::create('features', function (Blueprint $table) {
            $table->unsignedBigInteger('race_id');
            $table->unsignedTinyInteger('lane'); // race_entries.lane と対応
            $table->string('feature_version');
            $table->timestampTz('cutoff_at'); // 特徴量生成に用いたカットオフ時刻（NOT NULL）
            $table->jsonb('payload'); // 特徴量本体（NOT NULL）

            $table->timestamps();

            $table->primary(['race_id', 'lane', 'feature_version']);

            // race_entries は id を主キーとするが、(race_id, lane) に UNIQUE 制約があるため
            // 複合外部キーの参照先として利用できる。
            $table->foreign(['race_id', 'lane'])
                ->references(['race_id', 'lane'])
                ->on('race_entries')
                ->cascadeOnDelete();
        });

        // cutoff_at は races.deadline_at より前でなければならない（配信時刻の逆転を防止）。
        // races テーブルの値を参照する必要があり CHECK 制約では表現できないため、トリガーで強制する。
        DB::statement(<<<'SQL'
            CREATE OR REPLACE FUNCTION features_cutoff_before_deadline()
            RETURNS trigger AS $$
            DECLARE
                v_deadline_at timestamptz;
            BEGIN
                SELECT deadline_at INTO v_deadline_at FROM races WHERE id = NEW.race_id;

                IF v_deadline_at IS NULL THEN
                    RAISE EXCEPTION 'features: race_id=% not found in races', NEW.race_id;
                END IF;

                IF NEW.cutoff_at >= v_deadline_at THEN
                    RAISE EXCEPTION
                        'features: cutoff_at (%) must be before races.deadline_at (%) for race_id=%',
                        NEW.cutoff_at, v_deadline_at, NEW.race_id;
                END IF;

                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
        SQL);

        DB::statement(<<<'SQL'
            CREATE TRIGGER features_cutoff_before_deadline
                BEFORE INSERT OR UPDATE ON features
                FOR EACH ROW
                EXECUTE FUNCTION features_cutoff_before_deadline();
        SQL);
    }

    public function down(): void
    {
        DB::statement('DROP TRIGGER IF EXISTS features_cutoff_before_deadline ON features');
        DB::statement('DROP FUNCTION IF EXISTS features_cutoff_before_deadline()');
        Schema::dropIfExists('features');
    }
};
