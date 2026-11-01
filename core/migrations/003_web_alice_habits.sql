-- Веб-панель (вход через бота), Алиса, привычки, утренняя сводка, отделы в списке покупок.

-- Одноразовые ссылки для входа в веб-панель (хранится только хеш)
CREATE TABLE web_login_tokens (
    token_hash  text PRIMARY KEY,
    user_id     int NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at  timestamptz NOT NULL,
    used_at     timestamptz
);

-- Открытые сессии веб-панели
CREATE TABLE web_sessions (
    token_hash  text PRIMARY KEY,
    user_id     int NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at  timestamptz NOT NULL DEFAULT now(),
    expires_at  timestamptz NOT NULL,
    last_seen   timestamptz NOT NULL DEFAULT now(),
    user_agent  text
);

-- Коды привязки других каналов (Алиса и т.п.) к члену семьи
CREATE TABLE link_codes (
    code        text PRIMARY KEY,
    user_id     int NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    channel     text NOT NULL,
    expires_at  timestamptz NOT NULL
);

-- Привычки и показатели: вода (мл), шаги, вес (кг), сон (часы)
CREATE TABLE metrics (
    id          bigserial PRIMARY KEY,
    user_id     int NOT NULL REFERENCES users(id),
    kind        text NOT NULL CHECK (kind IN ('water', 'steps', 'weight', 'sleep')),
    value       numeric(10,2) NOT NULL CHECK (value > 0),
    measured_at timestamptz NOT NULL DEFAULT now(),
    source      text NOT NULL DEFAULT 'text',
    deleted_at  timestamptz
);
CREATE INDEX metrics_user ON metrics (user_id, kind, measured_at) WHERE deleted_at IS NULL;

CREATE TABLE metric_goals (
    user_id     int NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind        text NOT NULL,
    target      numeric(10,2) NOT NULL,
    PRIMARY KEY (user_id, kind)
);

-- Утренняя сводка: включена ли, своё время (null = MORNING_TIME из .env), дата последней отправки
ALTER TABLE users ADD COLUMN morning_enabled boolean NOT NULL DEFAULT true;
ALTER TABLE users ADD COLUMN morning_time time;
ALTER TABLE users ADD COLUMN morning_sent_on date;

-- Отдел магазина для группировки списка покупок
ALTER TABLE shopping_items ADD COLUMN dept text;

-- Для вопросов к данным
CREATE VIEW ro.metrics AS
SELECT m.measured_at, m.measured_at::date AS day, m.kind, m.value, u.name AS user_name
FROM metrics m JOIN users u ON u.id = m.user_id
WHERE m.deleted_at IS NULL AND u.family_id = current_setting('app.family_id')::int;

CREATE VIEW ro.metric_goals AS
SELECT g.kind, g.target, u.name AS user_name
FROM metric_goals g JOIN users u ON u.id = g.user_id
WHERE u.family_id = current_setting('app.family_id')::int;

GRANT SELECT ON ro.metrics, ro.metric_goals TO assistant_ro;
