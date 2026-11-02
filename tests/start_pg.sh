#!/usr/bin/env bash
# Тестовая PostgreSQL 16 в песочнице (для Claude): создать при первом запуске и запустить.
# Использование: bash tests/start_pg.sh   → затем bash tests/run_all.sh --fresh
# В GitHub Actions не нужен: там база — сервис postgres из .github/workflows/tests.yml.
set -euo pipefail
DIR=${PGTEST_DIR:-/tmp/pgtest}; PORT=${PGPORT:-55432}
BIN=$(ls -d /usr/lib/postgresql/16/bin 2>/dev/null || dirname "$(command -v pg_ctl)")
if ! [ -x "$BIN/pg_ctl" ]; then
  apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq postgresql-16 >/dev/null
  BIN=/usr/lib/postgresql/16/bin
fi
id postgres >/dev/null 2>&1 || useradd -r postgres
if [ ! -f "$DIR/data/PG_VERSION" ]; then
  mkdir -p "$DIR" && chown postgres "$DIR"
  su postgres -c "$BIN/initdb -D $DIR/data -A trust -E UTF8 --locale=C.UTF-8" >/dev/null
fi
pg_isready -h /tmp -p "$PORT" -q || su postgres -c "$BIN/pg_ctl -D $DIR/data -l $DIR/log -o '-p $PORT -k /tmp' start -w" >/dev/null
psql -h /tmp -p "$PORT" -U postgres -d postgres -qtAc "SELECT 1 FROM pg_roles WHERE rolname='assistant'" | grep -q 1 \
  || psql -h /tmp -p "$PORT" -U postgres -d postgres -qc "CREATE ROLE assistant LOGIN SUPERUSER"
echo "PostgreSQL готова: сокет /tmp, порт $PORT, пользователь assistant"
