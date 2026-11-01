#!/usr/bin/env bash
# Быстрая проверка, что всё работает:  bash scripts/check.sh
cd "$(dirname "$0")/.."
set -a; source .env; set +a

echo "== Контейнеры =="
docker compose ps
echo
echo "== Ядро =="
curl -s http://127.0.0.1:8100/health && echo
echo
echo "== Пользователи в базе =="
curl -s -H "X-API-Key: $CORE_API_KEY" http://127.0.0.1:8100/v1/users && echo
echo
echo "== Последние ошибки (если есть) =="
docker compose logs --since 1h core telegram 2>&1 | grep -iE "error|exception|traceback" | tail -n 15 || echo "нет"
echo
echo "== Бэкапы =="
ls -lh backups/last 2>/dev/null || echo "пока нет (первый будет ночью; сделать сейчас: bash scripts/backup-now.sh)"
