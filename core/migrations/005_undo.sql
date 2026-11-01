-- Отмена записей: время создания для показателей и индексы для журнала операций.
-- В metrics раньше было только measured_at; для старых записей берём его же.
ALTER TABLE metrics ADD COLUMN IF NOT EXISTS created_at timestamptz;
UPDATE metrics SET created_at = measured_at WHERE created_at IS NULL;
ALTER TABLE metrics ALTER COLUMN created_at SET DEFAULT now();
ALTER TABLE metrics ALTER COLUMN created_at SET NOT NULL;

CREATE INDEX IF NOT EXISTS transactions_user_created ON transactions (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS transactions_family_created ON transactions (family_id, created_at DESC);
CREATE INDEX IF NOT EXISTS meals_user_created ON meals (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS metrics_user_created ON metrics (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS shopping_family_created ON shopping_items (family_id, created_at DESC);
