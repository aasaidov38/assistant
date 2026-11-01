-- Начальная схема базы данных ассистента.
-- Новые изменения добавляйте отдельными файлами: 002_....sql, 003_....sql.
-- Уже применённые файлы не меняйте: ядро помнит, какие файлы выполнены.

CREATE TABLE families (
    id          serial PRIMARY KEY,
    name        text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE users (
    id          serial PRIMARY KEY,
    family_id   int NOT NULL REFERENCES families(id),
    name        text NOT NULL,
    kcal_goal   int,
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- Один человек может писать из разных каналов (Telegram, Алиса, веб...)
CREATE TABLE user_identities (
    channel     text NOT NULL,
    external_id text NOT NULL,
    user_id     int NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    PRIMARY KEY (channel, external_id)
);

CREATE TABLE categories (
    id          serial PRIMARY KEY,
    kind        text NOT NULL CHECK (kind IN ('expense', 'income')),
    name        text NOT NULL,
    emoji       text NOT NULL DEFAULT '',
    sort        int NOT NULL DEFAULT 100,
    is_active   boolean NOT NULL DEFAULT true,
    UNIQUE (kind, name)
);

INSERT INTO categories (kind, name, emoji, sort) VALUES
    ('expense', 'Продукты',          '🛒', 10),
    ('expense', 'Хозтовары',         '🧴', 20),
    ('expense', 'Топливо',           '⛽', 30),
    ('expense', 'Авто',              '🚗', 40),
    ('expense', 'Кредиты',           '🏦', 50),
    ('expense', 'ЖКХ',               '🏠', 60),
    ('expense', 'Связь и интернет',  '📱', 70),
    ('expense', 'Транспорт',         '🚌', 80),
    ('expense', 'Здоровье',          '💊', 90),
    ('expense', 'Одежда',            '👕', 100),
    ('expense', 'Дети',              '🧸', 110),
    ('expense', 'Кафе и рестораны',  '🍽', 120),
    ('expense', 'Развлечения',       '🎉', 130),
    ('expense', 'Подарки',           '🎁', 140),
    ('expense', 'Образование',       '📚', 150),
    ('expense', 'Прочее',            '📦', 999),
    ('income',  'Зарплата',          '💼', 10),
    ('income',  'Подработка',        '🛠', 20),
    ('income',  'Возврат',           '↩️', 30),
    ('income',  'Подарок',           '🎁', 40),
    ('income',  'Прочие доходы',     '💰', 999);

CREATE TABLE transactions (
    id           bigserial PRIMARY KEY,
    family_id    int NOT NULL REFERENCES families(id),
    user_id      int NOT NULL REFERENCES users(id),
    kind         text NOT NULL CHECK (kind IN ('expense', 'income')),
    amount       numeric(14,2) NOT NULL CHECK (amount > 0),
    currency     text NOT NULL DEFAULT 'RUB',
    category_id  int NOT NULL REFERENCES categories(id),
    merchant     text,
    note         text,
    occurred_on  date NOT NULL,
    source       text NOT NULL DEFAULT 'text',   -- text | voice | receipt | api
    group_id     uuid,                           -- строки одного чека
    raw          jsonb,
    created_at   timestamptz NOT NULL DEFAULT now(),
    deleted_at   timestamptz
);
CREATE INDEX transactions_family_date ON transactions (family_id, occurred_on) WHERE deleted_at IS NULL;

CREATE TABLE meals (
    id          bigserial PRIMARY KEY,
    user_id     int NOT NULL REFERENCES users(id),
    eaten_at    timestamptz NOT NULL DEFAULT now(),
    meal_type   text,                -- breakfast | lunch | dinner | snack
    title       text NOT NULL,
    grams       numeric(8,1),
    kcal        numeric(8,1) NOT NULL DEFAULT 0,
    protein     numeric(8,1) NOT NULL DEFAULT 0,
    fat         numeric(8,1) NOT NULL DEFAULT 0,
    carbs       numeric(8,1) NOT NULL DEFAULT 0,
    items       jsonb,
    source      text NOT NULL DEFAULT 'text',   -- text | voice | photo
    created_at  timestamptz NOT NULL DEFAULT now(),
    deleted_at  timestamptz
);
CREATE INDEX meals_user_date ON meals (user_id, eaten_at) WHERE deleted_at IS NULL;

CREATE TABLE tasks (
    id                  bigserial PRIMARY KEY,
    family_id           int NOT NULL REFERENCES families(id),
    user_id             int NOT NULL REFERENCES users(id),
    title               text NOT NULL,
    notes               text,
    due_at              timestamptz,
    all_day             boolean NOT NULL DEFAULT false,
    remind_at           timestamptz,
    reminded_at         timestamptz,
    status              text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'done', 'deleted')),
    ticktick_id         text,
    ticktick_project_id text,
    created_at          timestamptz NOT NULL DEFAULT now(),
    done_at             timestamptz
);
CREATE INDEX tasks_remind ON tasks (remind_at) WHERE status = 'open' AND reminded_at IS NULL;

-- Подключённые внешние сервисы (токены TickTick и т.п.)
CREATE TABLE integrations (
    user_id     int NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider    text NOT NULL,
    data        jsonb NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, provider)
);

-- Журнал событий. Из него события уходят в n8n (если задан N8N_WEBHOOK_URL).
CREATE TABLE events (
    id            bigserial PRIMARY KEY,
    type          text NOT NULL,
    payload       jsonb NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    delivered_at  timestamptz,
    attempts      int NOT NULL DEFAULT 0,
    last_error    text
);
CREATE INDEX events_pending ON events (id) WHERE delivered_at IS NULL;
