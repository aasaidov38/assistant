#!/usr/bin/env bash
# Создаёт чистую тестовую базу и применяет все миграции по порядку.
# Подключение — переменные PGHOST, PGPORT, PGUSER (по умолчанию песочница: /tmp, 55432, assistant).
set -euo pipefail
cd "$(dirname "$0")"
export PGHOST=${PGHOST:-/tmp} PGPORT=${PGPORT:-55432} PGUSER=${PGUSER:-assistant}
DB=${PGDATABASE:-assistant}
psql -X -q -d postgres -c "DROP DATABASE IF EXISTS $DB" -c "CREATE DATABASE $DB"
for f in ../core/migrations/*.sql; do
  psql -X -q -v ON_ERROR_STOP=1 -d "$DB" -c "BEGIN" -f "$f" -c "COMMIT" >/dev/null
  echo "✔ $(basename "$f")"
done
