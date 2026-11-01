#!/usr/bin/env bash
# Обновить ассистента после замены файлов кода:  bash scripts/update.sh
# Перед обновлением делает резервную копию базы.
set -euo pipefail
cd "$(dirname "$0")/.."
echo "== Резервная копия перед обновлением =="
docker compose exec backup /backup.sh >/dev/null && echo "✔ готово"
echo "== Пересборка и перезапуск =="
docker compose up -d --build
docker image prune -f >/dev/null
sleep 5
docker compose ps
