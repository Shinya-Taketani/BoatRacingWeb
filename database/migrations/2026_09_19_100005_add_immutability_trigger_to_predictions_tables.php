<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

return new class extends Migration
{
    public function up(): void
    {
        // CLAUDE.md 設計上の絶対ルール5: predictions は published_at 以降
        // イミュータブル。published_at は NOT NULL（作成時に必ず確定する）なので、
        // 実質「作成後は一切UPDATE不可」になる。racer_periods の
        // 不変化トリガー（valid_toだけ更新を許す）と違い、predictionsに
        // 「後から書き換えてよい列」は無いため、例外なくUPDATEを禁止する。
        DB::statement(<<<'SQL'
            CREATE OR REPLACE FUNCTION predictions_prevent_update()
            RETURNS trigger AS $$
            BEGIN
                IF OLD.published_at IS NOT NULL THEN
                    RAISE EXCEPTION
                        'predictions: immutable once published_at is set (id=%, published_at=%)',
                        OLD.id, OLD.published_at;
                END IF;

                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
        SQL);

        DB::statement(<<<'SQL'
            CREATE TRIGGER predictions_prevent_update
                BEFORE UPDATE ON predictions
                FOR EACH ROW
                EXECUTE FUNCTION predictions_prevent_update();
        SQL);

        // prediction_entries / prediction_tickets 自身は published_at を持たないため、
        // 親predictionのpublished_atを引いて同様に判定する。
        DB::statement(<<<'SQL'
            CREATE OR REPLACE FUNCTION prediction_entries_prevent_update()
            RETURNS trigger AS $$
            DECLARE
                v_published_at timestamptz;
            BEGIN
                SELECT published_at INTO v_published_at
                FROM predictions WHERE id = OLD.prediction_id;

                IF v_published_at IS NOT NULL THEN
                    RAISE EXCEPTION
                        'prediction_entries: immutable once parent prediction is published '
                        '(prediction_id=%, lane=%)',
                        OLD.prediction_id, OLD.lane;
                END IF;

                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
        SQL);

        DB::statement(<<<'SQL'
            CREATE TRIGGER prediction_entries_prevent_update
                BEFORE UPDATE ON prediction_entries
                FOR EACH ROW
                EXECUTE FUNCTION prediction_entries_prevent_update();
        SQL);

        DB::statement(<<<'SQL'
            CREATE OR REPLACE FUNCTION prediction_tickets_prevent_update()
            RETURNS trigger AS $$
            DECLARE
                v_published_at timestamptz;
            BEGIN
                SELECT published_at INTO v_published_at
                FROM predictions WHERE id = OLD.prediction_id;

                IF v_published_at IS NOT NULL THEN
                    RAISE EXCEPTION
                        'prediction_tickets: immutable once parent prediction is published '
                        '(id=%, prediction_id=%)',
                        OLD.id, OLD.prediction_id;
                END IF;

                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
        SQL);

        DB::statement(<<<'SQL'
            CREATE TRIGGER prediction_tickets_prevent_update
                BEFORE UPDATE ON prediction_tickets
                FOR EACH ROW
                EXECUTE FUNCTION prediction_tickets_prevent_update();
        SQL);
    }

    public function down(): void
    {
        DB::statement('DROP TRIGGER IF EXISTS prediction_tickets_prevent_update ON prediction_tickets');
        DB::statement('DROP FUNCTION IF EXISTS prediction_tickets_prevent_update()');

        DB::statement('DROP TRIGGER IF EXISTS prediction_entries_prevent_update ON prediction_entries');
        DB::statement('DROP FUNCTION IF EXISTS prediction_entries_prevent_update()');

        DB::statement('DROP TRIGGER IF EXISTS predictions_prevent_update ON predictions');
        DB::statement('DROP FUNCTION IF EXISTS predictions_prevent_update()');
    }
};
