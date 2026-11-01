-- Новые функции: лимиты бюджета, позиции чеков, список покупок, задачи для членов семьи,
-- недельный разбор и вопросы к своим данным.

-- Лимиты по категориям расходов (в месяц)
CREATE TABLE budgets (
    family_id     int NOT NULL REFERENCES families(id),
    category_id   int NOT NULL REFERENCES categories(id),
    monthly_limit numeric(14,2) NOT NULL CHECK (monthly_limit > 0),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (family_id, category_id)
);

-- Какие предупреждения о лимите уже отправлены в этом месяце (чтобы не повторять)
CREATE TABLE budget_alerts (
    family_id   int NOT NULL,
    category_id int NOT NULL,
    month       date NOT NULL,          -- первое число месяца
    level       int NOT NULL,           -- 80 или 100 (процентов)
    sent_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (family_id, category_id, month, level)
);

-- Позиции чека. Сумма по категории лежит в transactions, здесь детализация.
CREATE TABLE transaction_items (
    id             bigserial PRIMARY KEY,
    transaction_id bigint NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
    name           text NOT NULL,
    qty            numeric(10,3),
    amount         numeric(14,2) NOT NULL,
    subcategory    text                 -- группа товаров: молочное, мясо, сладости, алкоголь...
);
CREATE INDEX transaction_items_tx ON transaction_items (transaction_id);

-- Семейный список покупок
CREATE TABLE shopping_items (
    id          bigserial PRIMARY KEY,
    family_id   int NOT NULL REFERENCES families(id),
    title       text NOT NULL,
    qty         text,
    added_by    int REFERENCES users(id),
    created_at  timestamptz NOT NULL DEFAULT now(),
    bought_at   timestamptz,
    deleted_at  timestamptz
);
CREATE INDEX shopping_open ON shopping_items (family_id) WHERE bought_at IS NULL AND deleted_at IS NULL;

-- Ответы ИИ, к которым привязаны кнопки (рецепты и т.п.)
CREATE TABLE suggestions (
    id          bigserial PRIMARY KEY,
    user_id     int NOT NULL REFERENCES users(id),
    kind        text NOT NULL,
    data        jsonb NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- Задача может быть назначена другому члену семьи
ALTER TABLE tasks ADD COLUMN assignee_id int REFERENCES users(id);

-- Недельный разбор
ALTER TABLE users ADD COLUMN weekly_digest boolean NOT NULL DEFAULT true;
ALTER TABLE users ADD COLUMN digest_sent_on date;

-- ---------------------------------------------------------------
-- Только для чтения: представления, по которым ИИ отвечает на вопросы
-- («сколько потратили на продукты в сентябре»). Запросы выполняются
-- от роли assistant_ro, которая видит только эти представления и
-- только данные своей семьи.
-- ---------------------------------------------------------------
CREATE SCHEMA ro;

CREATE VIEW ro.transactions AS
SELECT t.id, t.occurred_on, t.kind, t.amount, c.name AS category, t.merchant, t.note,
       u.name AS user_name, t.source, t.created_at
FROM transactions t JOIN categories c ON c.id = t.category_id JOIN users u ON u.id = t.user_id
WHERE t.deleted_at IS NULL AND t.family_id = current_setting('app.family_id')::int;

CREATE VIEW ro.receipt_items AS
SELECT i.id, i.transaction_id, t.occurred_on, i.name, i.qty, i.amount, i.subcategory,
       c.name AS category, t.merchant, u.name AS user_name
FROM transaction_items i JOIN transactions t ON t.id = i.transaction_id
JOIN categories c ON c.id = t.category_id JOIN users u ON u.id = t.user_id
WHERE t.deleted_at IS NULL AND t.family_id = current_setting('app.family_id')::int;

CREATE VIEW ro.meals AS
SELECT m.id, m.eaten_at, m.eaten_at::date AS day, m.meal_type, m.title, m.grams,
       m.kcal, m.protein, m.fat, m.carbs, u.name AS user_name
FROM meals m JOIN users u ON u.id = m.user_id
WHERE m.deleted_at IS NULL AND u.family_id = current_setting('app.family_id')::int;

CREATE VIEW ro.tasks AS
SELECT t.id, t.title, t.notes, t.due_at, t.status, t.created_at, t.done_at,
       a.name AS author, coalesce(s.name, a.name) AS assignee
FROM tasks t JOIN users a ON a.id = t.user_id LEFT JOIN users s ON s.id = t.assignee_id
WHERE t.status <> 'deleted' AND t.family_id = current_setting('app.family_id')::int;

CREATE VIEW ro.budgets AS
SELECT c.name AS category, b.monthly_limit
FROM budgets b JOIN categories c ON c.id = b.category_id
WHERE b.family_id = current_setting('app.family_id')::int;

CREATE VIEW ro.shopping AS
SELECT s.title, s.qty, u.name AS added_by, s.created_at, s.bought_at
FROM shopping_items s LEFT JOIN users u ON u.id = s.added_by
WHERE s.deleted_at IS NULL AND s.family_id = current_setting('app.family_id')::int;

CREATE VIEW ro.users AS
SELECT u.name, u.kcal_goal FROM users u WHERE u.family_id = current_setting('app.family_id')::int;

CREATE VIEW ro.categories AS
SELECT kind, name FROM categories WHERE is_active;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'assistant_ro') THEN
        CREATE ROLE assistant_ro NOLOGIN;
    END IF;
END $$;
GRANT USAGE ON SCHEMA ro TO assistant_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA ro TO assistant_ro;
