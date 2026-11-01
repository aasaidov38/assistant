#!/usr/bin/env bash
# Сделать резервную копию базы прямо сейчас:  bash scripts/backup-now.sh
cd "$(dirname "$0")/.."
docker compose exec backup /backup.sh && ls -lh backups/last
