-- Версия 1.8: обновления по кнопке, скидочные карты, push-уведомления.

-- Служебные значения ядра: какая версия уже объявлена семье, о какой новой версии сообщили и т.п.
CREATE TABLE app_state (
    key         text PRIMARY KEY,
    value       jsonb NOT NULL DEFAULT '{}',
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- Скидочные карты магазинов. Номер карты лояльности — не банковская карта.
CREATE TABLE loyalty_cards (
    id          bigserial PRIMARY KEY,
    family_id   int NOT NULL REFERENCES families(id),
    user_id     int REFERENCES users(id),
    store       text NOT NULL,
    number      text NOT NULL,
    format      text NOT NULL DEFAULT 'code128' CHECK (format IN ('ean13', 'ean8', 'code128', 'qr', 'text')),
    note        text,
    used_count  int NOT NULL DEFAULT 0,
    used_at     timestamptz,
    created_at  timestamptz NOT NULL DEFAULT now(),
    deleted_at  timestamptz
);
CREATE INDEX loyalty_cards_family ON loyalty_cards (family_id) WHERE deleted_at IS NULL;

-- Подписки на push-уведомления (веб-приложение на iPhone, компьютере, Android)
CREATE TABLE push_subscriptions (
    id          bigserial PRIMARY KEY,
    user_id     int NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    endpoint    text NOT NULL UNIQUE,
    p256dh      text NOT NULL,
    auth        text NOT NULL,
    user_agent  text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    last_ok     timestamptz,
    fails       int NOT NULL DEFAULT 0
);
