#!/usr/bin/env bash
# Первичная подготовка: .env с паролями, сеть для n8n, папка бэкапов.
# Запуск из папки проекта:  bash scripts/setup.sh
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -f .env ]; then
  cp .env.example .env
  echo "✔ Создан файл .env"
fi

gen() { openssl rand -hex 24; }
if grep -q '^POSTGRES_PASSWORD=$' .env; then
  sed -i "s/^POSTGRES_PASSWORD=$/POSTGRES_PASSWORD=$(gen)/" .env
  echo "✔ Сгенерирован пароль базы данных"
fi
if grep -q '^CORE_API_KEY=$' .env; then
  sed -i "s/^CORE_API_KEY=$/CORE_API_KEY=$(gen)/" .env
  echo "✔ Сгенерирован ключ API ядра"
fi
chmod 600 .env

if ! docker network inspect assistant_shared >/dev/null 2>&1; then
  docker network create assistant_shared >/dev/null
  echo "✔ Создана сеть assistant_shared (для связи с n8n и Caddy)"
fi

mkdir -p backups
chown -R 999:999 backups 2>/dev/null || sudo chown -R 999:999 backups
echo "✔ Папка backups готова"

echo
echo "Дальше откройте настройки:  nano .env"
echo "и заполните TELEGRAM_BOT_TOKEN, ALLOWED_USERS, LLM_API_KEY (и по желанию STT_API_KEY)."
