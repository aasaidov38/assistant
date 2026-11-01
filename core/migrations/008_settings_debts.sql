-- Версия 1.7: настройки, личные и общие разделы, учёт долгов, тихие часы.

-- ---------- Настройки ----------
-- Личные настройки человека (тема, вкладки, уведомления, тихие часы...). Пустой объект = всё по умолчанию.
ALTER TABLE users ADD COLUMN settings jsonb NOT NULL DEFAULT '{}';
-- Свой день и час недельного разбора (пусто = WEEKLY_DIGEST_DAY / WEEKLY_DIGEST_HOUR из .env)
ALTER TABLE users ADD COLUMN digest_day smallint CHECK (digest_day BETWEEN 0 AND 6);
ALTER TABLE users ADD COLUMN digest_hour smallint CHECK (digest_hour BETWEEN 0 AND 23);
-- Режим разделов семьи: {"finance": "shared", "nutrition": "personal", ...}. Пусто = режим по умолчанию.
ALTER TABLE families ADD COLUMN sections jsonb NOT NULL DEFAULT '{}';

-- Сообщения, отложенные на тихие часы
CREATE TABLE outbox (
    id          bigserial PRIMARY KEY,
    user_id     int NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    reply       jsonb NOT NULL,
    send_after  timestamptz NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    sent_at     timestamptz
);
CREATE INDEX outbox_due ON outbox (send_after) WHERE sent_at IS NULL;

-- ---------- Долги ----------
-- direction: lent — я дал, мне должны; borrowed — я взял, я должен
CREATE TABLE debts (
    id           bigserial PRIMARY KEY,
    family_id    int NOT NULL REFERENCES families(id),
    user_id      int NOT NULL REFERENCES users(id),
    direction    text NOT NULL CHECK (direction IN ('lent', 'borrowed')),
    person       text NOT NULL,
    amount       numeric(14,2) NOT NULL CHECK (amount > 0),
    note         text,
    start_on     date NOT NULL DEFAULT current_date,
    due_on       date,
    snooze_until date,
    reminded     text[] NOT NULL DEFAULT '{}',
    created_at   timestamptz NOT NULL DEFAULT now(),
    deleted_at   timestamptz
);
CREATE INDEX debts_family ON debts (family_id) WHERE deleted_at IS NULL;

-- Движения по долгу: repay — вернули часть, add — заняли ещё, forgive — простили остаток
CREATE TABLE debt_moves (
    id          bigserial PRIMARY KEY,
    debt_id     bigint NOT NULL REFERENCES debts(id) ON DELETE CASCADE,
    user_id     int REFERENCES users(id),
    kind        text NOT NULL DEFAULT 'repay' CHECK (kind IN ('repay', 'add', 'forgive')),
    amount      numeric(14,2) NOT NULL CHECK (amount > 0),
    occurred_on date NOT NULL DEFAULT current_date,
    note        text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    deleted_at  timestamptz
);
CREATE INDEX debt_moves_debt ON debt_moves (debt_id) WHERE deleted_at IS NULL;

-- ---------- Вопросы к данным с учётом личных разделов ----------
-- Видна ли запись человеку, который спрашивает (app.user_id): в общем разделе — всем в семье,
-- в личном — только автору. Функция читает настройки семьи от имени владельца базы.
CREATE FUNCTION ro.can_see(section text, owner int) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT CASE
    WHEN coalesce((SELECT f.sections ->> section FROM families f
                    WHERE f.id = current_setting('app.family_id')::int),
                  CASE WHEN section IN ('nutrition', 'habits', 'meds', 'notes', 'debts') THEN 'personal' ELSE 'shared' END)
         = 'shared' THEN true
    ELSE owner IS NOT DISTINCT FROM nullif(current_setting('app.user_id', true), '')::int
  END
$$;
REVOKE ALL ON FUNCTION ro.can_see(text, int) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION ro.can_see(text, int) TO assistant_ro;

CREATE OR REPLACE VIEW ro.transactions AS
SELECT t.id, t.occurred_on, t.kind, t.amount, c.name AS category, t.merchant, t.note,
       u.name AS user_name, t.source, t.created_at, v.name AS vehicle, tr.title AS trip
FROM transactions t JOIN categories c ON c.id = t.category_id JOIN users u ON u.id = t.user_id
LEFT JOIN vehicles v ON v.id = t.vehicle_id
LEFT JOIN trips tr ON tr.id = t.trip_id
WHERE t.deleted_at IS NULL AND t.family_id = current_setting('app.family_id')::int AND ro.can_see('finance', t.user_id);

CREATE OR REPLACE VIEW ro.receipt_items AS
SELECT i.id, i.transaction_id, t.occurred_on, i.name, i.qty, i.amount, i.subcategory, c.name AS category,
       t.merchant, u.name AS user_name
FROM transaction_items i JOIN transactions t ON t.id = i.transaction_id
JOIN categories c ON c.id = t.category_id JOIN users u ON u.id = t.user_id
WHERE t.deleted_at IS NULL AND t.family_id = current_setting('app.family_id')::int AND ro.can_see('finance', t.user_id);

CREATE OR REPLACE VIEW ro.meals AS
SELECT m.id, m.eaten_at, m.eaten_at::date AS day, m.meal_type, m.title, m.grams, m.kcal, m.protein, m.fat, m.carbs,
       u.name AS user_name
FROM meals m JOIN users u ON u.id = m.user_id
WHERE m.deleted_at IS NULL AND u.family_id = current_setting('app.family_id')::int AND ro.can_see('nutrition', m.user_id);

CREATE OR REPLACE VIEW ro.metrics AS
SELECT m.measured_at, m.measured_at::date AS day, m.kind, m.value, u.name AS user_name
FROM metrics m JOIN users u ON u.id = m.user_id
WHERE m.deleted_at IS NULL AND u.family_id = current_setting('app.family_id')::int AND ro.can_see('habits', m.user_id);

CREATE OR REPLACE VIEW ro.metric_goals AS
SELECT g.kind, g.target, u.name AS user_name
FROM metric_goals g JOIN users u ON u.id = g.user_id
WHERE u.family_id = current_setting('app.family_id')::int AND ro.can_see('habits', g.user_id);

CREATE OR REPLACE VIEW ro.shopping AS
SELECT s.title, s.qty, u.name AS added_by, s.created_at, s.bought_at
FROM shopping_items s LEFT JOIN users u ON u.id = s.added_by
WHERE s.deleted_at IS NULL AND s.family_id = current_setting('app.family_id')::int AND ro.can_see('shopping', s.added_by);

CREATE OR REPLACE VIEW ro.bills AS
SELECT b.title, b.amount, c.name AS category, b.due_day, b.period_months, b.next_due
FROM bills b LEFT JOIN categories c ON c.id = b.category_id
WHERE b.deleted_at IS NULL AND b.family_id = current_setting('app.family_id')::int AND ro.can_see('bills', b.user_id);

CREATE OR REPLACE VIEW ro.savings AS
SELECT g.title AS goal, g.target, g.deadline, m.amount, m.created_at, u.name AS user_name
FROM savings_moves m JOIN savings_goals g ON g.id = m.goal_id LEFT JOIN users u ON u.id = m.user_id
WHERE m.deleted_at IS NULL AND g.deleted_at IS NULL AND g.family_id = current_setting('app.family_id')::int
  AND ro.can_see('savings', g.user_id);

CREATE OR REPLACE VIEW ro.vehicles AS
SELECT v.id, v.name, v.kind, v.mileage FROM vehicles v
WHERE v.deleted_at IS NULL AND v.family_id = current_setting('app.family_id')::int AND ro.can_see('garage', v.user_id);

CREATE OR REPLACE VIEW ro.service_log AS
SELECT v.name AS vehicle, s.title, s.km, s.done_on, s.cost
FROM service_log s JOIN vehicles v ON v.id = s.vehicle_id
WHERE s.deleted_at IS NULL AND v.family_id = current_setting('app.family_id')::int AND ro.can_see('garage', v.user_id);

CREATE OR REPLACE VIEW ro.papers AS
SELECT p.kind, p.title, p.owner, p.expires_on, p.purchased_on
FROM papers p WHERE p.deleted_at IS NULL AND p.family_id = current_setting('app.family_id')::int
  AND ro.can_see('papers', p.user_id);

CREATE OR REPLACE VIEW ro.meter_readings AS
SELECT m.title AS meter, m.kind, m.unit, r.value, r.created_at, r.created_at::date AS day
FROM meter_readings r JOIN meters m ON m.id = r.meter_id
WHERE r.deleted_at IS NULL AND m.deleted_at IS NULL AND m.family_id = current_setting('app.family_id')::int
  AND ro.can_see('meters', m.user_id);

CREATE OR REPLACE VIEW ro.meds AS
SELECT m.title, m.dose, m.for_whom, u.name AS user_name, m.times, m.start_on, m.end_on, m.stock, m.active
FROM meds m JOIN users u ON u.id = m.user_id
WHERE m.deleted_at IS NULL AND m.family_id = current_setting('app.family_id')::int AND ro.can_see('meds', m.user_id);

CREATE OR REPLACE VIEW ro.med_intakes AS
SELECT m.title, m.for_whom, i.due_at, i.status, i.taken_at
FROM med_intakes i JOIN meds m ON m.id = i.med_id
WHERE m.deleted_at IS NULL AND m.family_id = current_setting('app.family_id')::int AND ro.can_see('meds', m.user_id);

CREATE OR REPLACE VIEW ro.trips AS
SELECT t.id, t.title, t.kind, t.start_on, t.end_on, t.budget,
       (SELECT coalesce(sum(x.amount), 0) FROM transactions x
         WHERE x.trip_id = t.id AND x.deleted_at IS NULL AND x.kind = 'expense') AS spent
FROM trips t WHERE t.deleted_at IS NULL AND t.family_id = current_setting('app.family_id')::int
  AND ro.can_see('trips', t.user_id);

CREATE VIEW ro.debts AS
SELECT CASE d.direction WHEN 'lent' THEN 'мне должны' ELSE 'я должен' END AS direction,
       d.person, d.amount, d.start_on, d.due_on, d.note, u.name AS user_name,
       coalesce(sum(CASE WHEN m.kind = 'add' THEN m.amount END), 0) AS added,
       coalesce(sum(CASE WHEN m.kind IN ('repay', 'forgive') THEN m.amount END), 0) AS returned,
       d.amount + coalesce(sum(CASE WHEN m.kind = 'add' THEN m.amount END), 0)
                - coalesce(sum(CASE WHEN m.kind IN ('repay', 'forgive') THEN m.amount END), 0) AS balance
FROM debts d JOIN users u ON u.id = d.user_id
LEFT JOIN debt_moves m ON m.debt_id = d.id AND m.deleted_at IS NULL
WHERE d.deleted_at IS NULL AND d.family_id = current_setting('app.family_id')::int AND ro.can_see('debts', d.user_id)
GROUP BY d.id, u.name;
GRANT SELECT ON ro.debts TO assistant_ro;
