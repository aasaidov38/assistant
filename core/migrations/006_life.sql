-- Версия 1.5: регулярные платежи, импорт выписок, накопления, гараж, документы и гарантии,
-- счётчики, семейные заметки, лекарства.

-- ---------- Файлы (фото гарантийных талонов, полисов, счётчиков) ----------
CREATE TABLE attachments (
    id          bigserial PRIMARY KEY,
    family_id   int NOT NULL REFERENCES families(id),
    mime        text NOT NULL,
    data        bytea NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- ---------- 1. Регулярные платежи ----------
CREATE TABLE bills (
    id               bigserial PRIMARY KEY,
    family_id        int NOT NULL REFERENCES families(id),
    user_id          int REFERENCES users(id),
    title            text NOT NULL,
    amount           numeric(14,2) CHECK (amount > 0),
    category_id      int REFERENCES categories(id),
    due_day          int NOT NULL CHECK (due_day BETWEEN 1 AND 31),
    period_months    int NOT NULL DEFAULT 1 CHECK (period_months BETWEEN 1 AND 24),
    next_due         date NOT NULL,
    remind_days      int NOT NULL DEFAULT 3 CHECK (remind_days BETWEEN 0 AND 30),
    last_reminded_on date,
    created_at       timestamptz NOT NULL DEFAULT now(),
    deleted_at       timestamptz
);
CREATE INDEX bills_family ON bills (family_id, next_due) WHERE deleted_at IS NULL;

CREATE TABLE bill_payments (
    id              bigserial PRIMARY KEY,
    bill_id         bigint NOT NULL REFERENCES bills(id) ON DELETE CASCADE,
    due_on          date NOT NULL,                 -- за какой срок платёж
    transaction_id  bigint REFERENCES transactions(id),
    skipped         boolean NOT NULL DEFAULT false, -- «пропустить этот месяц» без траты
    user_id         int REFERENCES users(id),
    created_at      timestamptz NOT NULL DEFAULT now(),
    deleted_at      timestamptz
);
CREATE INDEX bill_payments_tx ON bill_payments (transaction_id);

-- ---------- 2. Импорт банковских выписок ----------
CREATE TABLE bank_imports (
    id          bigserial PRIMARY KEY,
    family_id   int NOT NULL REFERENCES families(id),
    user_id     int REFERENCES users(id),
    filename    text,
    bank        text,
    date_from   date,
    date_to     date,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE bank_rows (
    id              bigserial PRIMARY KEY,
    import_id       bigint NOT NULL REFERENCES bank_imports(id) ON DELETE CASCADE,
    op_date         date NOT NULL,
    kind            text NOT NULL CHECK (kind IN ('expense', 'income')),
    amount          numeric(14,2) NOT NULL CHECK (amount > 0),
    description     text,
    bank_category   text,
    category        text,                         -- предложение ИИ
    is_transfer     boolean NOT NULL DEFAULT false,  -- перевод между своими счетами
    status          text NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'matched', 'added', 'skipped')),
    transaction_id  bigint REFERENCES transactions(id)
);
CREATE INDEX bank_rows_import ON bank_rows (import_id);

-- ---------- 3. Накопления ----------
CREATE TABLE savings_goals (
    id          bigserial PRIMARY KEY,
    family_id   int NOT NULL REFERENCES families(id),
    user_id     int REFERENCES users(id),
    title       text NOT NULL,
    target      numeric(14,2) NOT NULL CHECK (target > 0),
    deadline    date,
    created_at  timestamptz NOT NULL DEFAULT now(),
    closed_at   timestamptz,
    deleted_at  timestamptz
);
CREATE TABLE savings_moves (
    id          bigserial PRIMARY KEY,
    goal_id     bigint NOT NULL REFERENCES savings_goals(id) ON DELETE CASCADE,
    user_id     int REFERENCES users(id),
    amount      numeric(14,2) NOT NULL CHECK (amount <> 0),   -- минус = сняли
    note        text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    deleted_at  timestamptz
);
CREATE INDEX savings_moves_goal ON savings_moves (goal_id) WHERE deleted_at IS NULL;

-- ---------- 4. Гараж ----------
CREATE TABLE vehicles (
    id          bigserial PRIMARY KEY,
    family_id   int NOT NULL REFERENCES families(id),
    user_id     int REFERENCES users(id),
    name        text NOT NULL,
    kind        text NOT NULL DEFAULT 'car' CHECK (kind IN ('car', 'moto')),
    mileage     int,
    mileage_at  timestamptz,
    asked_at    timestamptz,                      -- когда последний раз спрашивали пробег
    created_at  timestamptz NOT NULL DEFAULT now(),
    deleted_at  timestamptz
);
CREATE TABLE odometer (
    id          bigserial PRIMARY KEY,
    vehicle_id  bigint NOT NULL REFERENCES vehicles(id) ON DELETE CASCADE,
    km          int NOT NULL CHECK (km >= 0),
    user_id     int REFERENCES users(id),
    created_at  timestamptz NOT NULL DEFAULT now(),
    deleted_at  timestamptz
);
CREATE TABLE service_rules (
    id            bigserial PRIMARY KEY,
    vehicle_id    bigint NOT NULL REFERENCES vehicles(id) ON DELETE CASCADE,
    title         text NOT NULL,
    every_km      int CHECK (every_km > 0),
    every_months  int CHECK (every_months > 0),
    last_km       int,
    last_date     date,
    reminded_at   timestamptz,
    snooze_until  date,
    created_at    timestamptz NOT NULL DEFAULT now(),
    deleted_at    timestamptz
);
CREATE TABLE service_log (
    id              bigserial PRIMARY KEY,
    vehicle_id      bigint NOT NULL REFERENCES vehicles(id) ON DELETE CASCADE,
    rule_id         bigint REFERENCES service_rules(id),
    title           text NOT NULL,
    km              int,
    done_on         date NOT NULL,
    cost            numeric(14,2),
    transaction_id  bigint REFERENCES transactions(id),
    prev_km         int,                 -- что было в регламенте до этой записи (для отмены)
    prev_date       date,
    user_id         int REFERENCES users(id),
    created_at      timestamptz NOT NULL DEFAULT now(),
    deleted_at      timestamptz
);
CREATE TABLE season_sent (
    vehicle_id  bigint NOT NULL REFERENCES vehicles(id) ON DELETE CASCADE,
    key         text NOT NULL,
    year        int NOT NULL,
    PRIMARY KEY (vehicle_id, key, year)
);
-- Трата может относиться к машине или мотоциклу (топливо, ремонт)
ALTER TABLE transactions ADD COLUMN vehicle_id bigint REFERENCES vehicles(id);

-- ---------- 5. Документы, полисы, гарантии ----------
CREATE TABLE papers (
    id             bigserial PRIMARY KEY,
    family_id      int NOT NULL REFERENCES families(id),
    user_id        int REFERENCES users(id),
    kind           text NOT NULL DEFAULT 'document' CHECK (kind IN ('document', 'policy', 'warranty')),
    title          text NOT NULL,
    owner          text,                 -- чей: «Яна», «младшая», «машина»
    vehicle_id     bigint REFERENCES vehicles(id),
    expires_on     date,
    purchased_on   date,
    note           text,
    attachment_id  bigint REFERENCES attachments(id),
    reminded_days  int,                  -- последний порог напоминания (90, 30, 7...)
    created_at     timestamptz NOT NULL DEFAULT now(),
    deleted_at     timestamptz
);
CREATE INDEX papers_family ON papers (family_id, expires_on) WHERE deleted_at IS NULL;

-- ---------- 6. Счётчики ----------
CREATE TABLE meters (
    id              bigserial PRIMARY KEY,
    family_id       int NOT NULL REFERENCES families(id),
    user_id         int REFERENCES users(id),
    title           text NOT NULL,
    kind            text NOT NULL DEFAULT 'other'
                    CHECK (kind IN ('cold_water', 'hot_water', 'electricity', 'gas', 'heat', 'other')),
    unit            text NOT NULL DEFAULT 'м³',
    verify_until    date,
    verify_reminded int,
    submit_from     int NOT NULL DEFAULT 20 CHECK (submit_from BETWEEN 1 AND 28),
    submit_to       int NOT NULL DEFAULT 25 CHECK (submit_to BETWEEN 1 AND 31),
    created_at      timestamptz NOT NULL DEFAULT now(),
    deleted_at      timestamptz
);
CREATE TABLE meter_readings (
    id             bigserial PRIMARY KEY,
    family_id      int NOT NULL REFERENCES families(id),
    meter_id       bigint REFERENCES meters(id) ON DELETE CASCADE,   -- пусто: ещё не выбрали, чей счётчик
    value          numeric(12,3) NOT NULL CHECK (value >= 0),
    user_id        int REFERENCES users(id),
    attachment_id  bigint REFERENCES attachments(id),
    created_at     timestamptz NOT NULL DEFAULT now(),
    deleted_at     timestamptz
);
CREATE INDEX meter_readings_meter ON meter_readings (meter_id, created_at) WHERE deleted_at IS NULL;
CREATE TABLE meter_reminders (
    family_id  int NOT NULL REFERENCES families(id),
    month      date NOT NULL,
    stage      int NOT NULL,
    PRIMARY KEY (family_id, month, stage)
);

-- ---------- 7. Семейные заметки ----------
CREATE TABLE family_notes (
    id          bigserial PRIMARY KEY,
    family_id   int NOT NULL REFERENCES families(id),
    user_id     int REFERENCES users(id),
    subject     text,
    text        text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    deleted_at  timestamptz
);
CREATE INDEX family_notes_family ON family_notes (family_id) WHERE deleted_at IS NULL;

-- ---------- 8. Лекарства ----------
CREATE TABLE meds (
    id              bigserial PRIMARY KEY,
    family_id       int NOT NULL REFERENCES families(id),
    user_id         int NOT NULL REFERENCES users(id),   -- кому приходят напоминания
    for_whom        text,                                 -- для кого, если не для себя: «младшая»
    title           text NOT NULL,
    dose            text,                                 -- как записал пользователь: «1 таблетка», «5 мл»
    times           time[] NOT NULL,
    days            int[],                                -- дни недели 0=пн…6=вс; пусто = каждый день
    start_on        date NOT NULL DEFAULT current_date,
    end_on          date,
    stock           numeric(10,2),
    per_intake      numeric(10,2) NOT NULL DEFAULT 1,
    unit            text NOT NULL DEFAULT 'шт',
    low_days        int NOT NULL DEFAULT 5,
    low_notified_on date,
    active          boolean NOT NULL DEFAULT true,
    created_at      timestamptz NOT NULL DEFAULT now(),
    deleted_at      timestamptz
);
CREATE TABLE med_intakes (
    id            bigserial PRIMARY KEY,
    med_id        bigint NOT NULL REFERENCES meds(id) ON DELETE CASCADE,
    due_at        timestamptz NOT NULL,
    status        text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'taken', 'skipped')),
    reminded      int NOT NULL DEFAULT 0,
    last_sent_at  timestamptz,
    snooze_until  timestamptz,
    taken_at      timestamptz,
    user_id       int REFERENCES users(id),
    UNIQUE (med_id, due_at)
);
CREATE INDEX med_intakes_pending ON med_intakes (status) WHERE status = 'pending';

-- ---------- Для вопросов к данным (только чтение, только своя семья) ----------
CREATE VIEW ro.bills AS
SELECT b.title, b.amount, c.name AS category, b.due_day, b.period_months, b.next_due
FROM bills b LEFT JOIN categories c ON c.id = b.category_id
WHERE b.deleted_at IS NULL AND b.family_id = current_setting('app.family_id')::int;

CREATE VIEW ro.savings AS
SELECT g.title AS goal, g.target, g.deadline, m.amount, m.created_at, u.name AS user_name
FROM savings_moves m JOIN savings_goals g ON g.id = m.goal_id LEFT JOIN users u ON u.id = m.user_id
WHERE m.deleted_at IS NULL AND g.deleted_at IS NULL AND g.family_id = current_setting('app.family_id')::int;

CREATE VIEW ro.vehicles AS
SELECT v.id, v.name, v.kind, v.mileage FROM vehicles v
WHERE v.deleted_at IS NULL AND v.family_id = current_setting('app.family_id')::int;

CREATE VIEW ro.service_log AS
SELECT v.name AS vehicle, s.title, s.km, s.done_on, s.cost
FROM service_log s JOIN vehicles v ON v.id = s.vehicle_id
WHERE s.deleted_at IS NULL AND v.family_id = current_setting('app.family_id')::int;

CREATE VIEW ro.papers AS
SELECT p.kind, p.title, p.owner, p.expires_on, p.purchased_on
FROM papers p WHERE p.deleted_at IS NULL AND p.family_id = current_setting('app.family_id')::int;

CREATE VIEW ro.meter_readings AS
SELECT m.title AS meter, m.kind, m.unit, r.value, r.created_at, r.created_at::date AS day
FROM meter_readings r JOIN meters m ON m.id = r.meter_id
WHERE r.deleted_at IS NULL AND m.deleted_at IS NULL AND m.family_id = current_setting('app.family_id')::int;

CREATE VIEW ro.meds AS
SELECT m.title, m.dose, m.for_whom, u.name AS user_name, m.times, m.start_on, m.end_on, m.stock, m.active
FROM meds m JOIN users u ON u.id = m.user_id
WHERE m.deleted_at IS NULL AND m.family_id = current_setting('app.family_id')::int;

CREATE VIEW ro.med_intakes AS
SELECT m.title, m.for_whom, i.due_at, i.status, i.taken_at
FROM med_intakes i JOIN meds m ON m.id = i.med_id
WHERE m.deleted_at IS NULL AND m.family_id = current_setting('app.family_id')::int;

GRANT SELECT ON ro.bills, ro.savings, ro.vehicles, ro.service_log, ro.papers, ro.meter_readings,
                ro.meds, ro.med_intakes TO assistant_ro;

-- ro.transactions: добавить технику, к которой относится трата
CREATE OR REPLACE VIEW ro.transactions AS
SELECT t.id, t.occurred_on, t.kind, t.amount, c.name AS category, t.merchant, t.note,
       u.name AS user_name, t.source, t.created_at, v.name AS vehicle
FROM transactions t JOIN categories c ON c.id = t.category_id JOIN users u ON u.id = t.user_id
LEFT JOIN vehicles v ON v.id = t.vehicle_id
WHERE t.deleted_at IS NULL AND t.family_id = current_setting('app.family_id')::int;
