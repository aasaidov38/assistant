-- Двусторонняя синхронизация с TickTick.

-- Откуда задача: bot (бот, Алиса, веб) или ticktick (создана в TickTick)
ALTER TABLE tasks ADD COLUMN source text NOT NULL DEFAULT 'bot';
ALTER TABLE tasks ADD COLUMN updated_at timestamptz;
ALTER TABLE tasks ADD COLUMN ticktick_list text;          -- название списка в TickTick
CREATE UNIQUE INDEX tasks_ticktick_id ON tasks (ticktick_id) WHERE ticktick_id IS NOT NULL;
