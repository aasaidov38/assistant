#!/usr/bin/env bash
# Восстановить базу из копии.
#   bash scripts/restore.sh                       — из последней копии
#   bash scripts/restore.sh backups/daily/xxx.sql.gz — из выбранного файла
# ВНИМАНИЕ: текущие данные в базе будут заменены.
set -euo pipefail
cd "$(dirname "$0")/.."
FILE="${1:-backups/last/assistant-latest.sql.gz}"
[ -f "$FILE" ] || { echo "Нет файла $FILE"; exit 1; }

read -r -p "Заменить данные копией $FILE? Введите yes: " ok
[ "$ok" = "yes" ] || { echo "Отменено"; exit 0; }

docker compose stop core telegram
docker compose exec -T db psql -U assistant -d postgres -c "DROP DATABASE IF EXISTS assistant WITH (FORCE);"
docker compose exec -T db psql -U assistant -d postgres -c "CREATE DATABASE assistant OWNER assistant;"
# роль только для чтения (для вопросов к данным); на новом сервере её ещё нет
docker compose exec -T db psql -U assistant -d postgres -c "DO \$\$BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='assistant_ro') THEN CREATE ROLE assistant_ro NOLOGIN; END IF; END\$\$;"
gunzip -c "$FILE" | docker compose exec -T db psql -q -U assistant -d assistant
docker compose start core telegram
echo "✔ Восстановлено из $FILE"
