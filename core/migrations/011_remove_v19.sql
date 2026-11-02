-- Откат доработок 1.9 (данные с телефона: здоровье, уведомления банков, баланс SIM).
-- Безопасно и там, где миграция 010 не применялась: везде IF EXISTS.
-- Записи шагов, сна и веса, которые приходили с телефона, остаются (это обычные записи привычек); пульс удаляется.
DELETE FROM metrics WHERE kind = 'pulse';
ALTER TABLE metrics DROP CONSTRAINT IF EXISTS metrics_kind_check;
ALTER TABLE metrics ADD CONSTRAINT metrics_kind_check CHECK (kind IN ('water', 'steps', 'weight', 'sleep'));
DROP TABLE IF EXISTS sim_cards;
DROP TABLE IF EXISTS bank_events;
DROP TABLE IF EXISTS devices;
UPDATE users SET settings = settings - 'bank_auto' - 'sim_low' WHERE settings IS NOT NULL;
