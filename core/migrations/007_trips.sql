-- Версия 1.5: режим поездки — бюджет, траты поездки, чек-лист сборов, погода по маршруту.

CREATE TABLE trips (
    id          bigserial PRIMARY KEY,
    family_id   int NOT NULL REFERENCES families(id),
    user_id     int REFERENCES users(id),
    title       text NOT NULL,
    kind        text NOT NULL DEFAULT 'car' CHECK (kind IN ('car', 'moto', 'plane', 'train', 'other')),
    start_on    date NOT NULL,
    end_on      date NOT NULL,
    budget      numeric(14,2),
    reminded    text[] NOT NULL DEFAULT '{}',     -- какие напоминания уже отправлены: 'before3', 'before1'
    created_at  timestamptz NOT NULL DEFAULT now(),
    deleted_at  timestamptz,
    CHECK (end_on >= start_on)
);
CREATE INDEX trips_family ON trips (family_id, start_on) WHERE deleted_at IS NULL;

-- Точки маршрута (координаты находятся по названию через бесплатный геокодер Open-Meteo)
CREATE TABLE trip_places (
    id       bigserial PRIMARY KEY,
    trip_id  bigint NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    name     text NOT NULL,
    lat      double precision,
    lon      double precision,
    sort     int NOT NULL DEFAULT 0
);

CREATE TABLE trip_checklist (
    id       bigserial PRIMARY KEY,
    trip_id  bigint NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
    title    text NOT NULL,
    done     boolean NOT NULL DEFAULT false,
    sort     int NOT NULL DEFAULT 0
);

-- Трата относится к поездке (ставится сама, если дата траты попадает в даты поездки)
ALTER TABLE transactions ADD COLUMN trip_id bigint REFERENCES trips(id);
CREATE INDEX transactions_trip ON transactions (trip_id) WHERE trip_id IS NOT NULL;

CREATE VIEW ro.trips AS
SELECT t.id, t.title, t.kind, t.start_on, t.end_on, t.budget,
       (SELECT coalesce(sum(x.amount), 0) FROM transactions x
         WHERE x.trip_id = t.id AND x.deleted_at IS NULL AND x.kind = 'expense') AS spent
FROM trips t WHERE t.deleted_at IS NULL AND t.family_id = current_setting('app.family_id')::int;
GRANT SELECT ON ro.trips TO assistant_ro;

CREATE OR REPLACE VIEW ro.transactions AS
SELECT t.id, t.occurred_on, t.kind, t.amount, c.name AS category, t.merchant, t.note,
       u.name AS user_name, t.source, t.created_at, v.name AS vehicle, tr.title AS trip
FROM transactions t JOIN categories c ON c.id = t.category_id JOIN users u ON u.id = t.user_id
LEFT JOIN vehicles v ON v.id = t.vehicle_id
LEFT JOIN trips tr ON tr.id = t.trip_id
WHERE t.deleted_at IS NULL AND t.family_id = current_setting('app.family_id')::int;
