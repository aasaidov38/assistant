-- Версия 1.9: данные с телефона — здоровье (Health Connect), уведомления и SMS банков, баланс SIM.

-- Телефоны, которым разрешено присылать данные. В базе только хеш ключа; ключ видит лишь само приложение.
CREATE TABLE devices (
    id          bigserial PRIMARY KEY,
    user_id     int NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name        text NOT NULL,
    key_hash    text NOT NULL UNIQUE,
    info        jsonb NOT NULL DEFAULT '{}',          -- версия приложения, Android, какие разрешения выданы
    created_at  timestamptz NOT NULL DEFAULT now(),
    last_seen   timestamptz,
    revoked_at  timestamptz
);
CREATE INDEX devices_user ON devices (user_id) WHERE revoked_at IS NULL;

-- Пульс из Health Connect и связь записей с исходными данными телефона (чтобы повторная отправка не задваивала)
ALTER TABLE metrics DROP CONSTRAINT IF EXISTS metrics_kind_check;
ALTER TABLE metrics ADD CONSTRAINT metrics_kind_check CHECK (kind IN ('water', 'steps', 'weight', 'sleep', 'pulse'));
ALTER TABLE metrics ADD COLUMN ext_id text;
ALTER TABLE metrics ADD COLUMN extra jsonb;
CREATE UNIQUE INDEX metrics_ext ON metrics (user_id, ext_id) WHERE ext_id IS NOT NULL;

-- Уведомления и SMS банков. Текст хранится обезличенным: длинные номера заменены, от карты — только 4 последние цифры.
CREATE TABLE bank_events (
    id             bigserial PRIMARY KEY,
    family_id      int NOT NULL REFERENCES families(id),
    user_id        int NOT NULL REFERENCES users(id),
    device_id      bigint REFERENCES devices(id) ON DELETE SET NULL,
    bank           text NOT NULL,                    -- sber | tbank | vtb | alfa
    channel        text NOT NULL,                    -- push | sms
    kind           text,                             -- expense | income | null (только остаток)
    amount         numeric(14,2),
    balance        numeric(14,2),
    card_tail      text CHECK (card_tail ~ '^\d{4}$'),
    merchant       text,
    text           text NOT NULL,
    text_hash      text NOT NULL,
    occurred_at    timestamptz NOT NULL,
    status         text NOT NULL,                    -- saved | matched | duplicate | transfer | balance | unparsed
    transaction_id bigint REFERENCES transactions(id) ON DELETE SET NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (user_id, text_hash)
);
CREATE INDEX bank_events_family ON bank_events (family_id, occurred_at DESC);

-- SIM-карты телефонов и их баланс
CREATE TABLE sim_cards (
    id          bigserial PRIMARY KEY,
    device_id   bigint NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    slot        int NOT NULL,
    operator    text NOT NULL,
    balance     numeric(12,2),
    error       text,
    checked_at  timestamptz NOT NULL DEFAULT now(),
    low_sent_at timestamptz,
    UNIQUE (device_id, slot)
);
