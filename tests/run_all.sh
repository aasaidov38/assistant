#!/usr/bin/env bash
# Все проверки ядра на настоящей PostgreSQL. Использование: bash tests/run_all.sh [--fresh]
#   --fresh — пересоздать тестовую базу и применить миграции заново.
set -uo pipefail
cd "$(dirname "$0")"
[ "${1:-}" = "--fresh" ] && bash setup_db.sh
python3 -m py_compile ../core/app/*.py ../adapters/telegram/*.py || exit 1
FAIL=0
for t in test_undo test_web test_life test_v17 test_v18 test_v19; do
  [ -f "$t.py" ] || continue
  echo "== $t"
  OUT=$(TZ=Asia/Irkutsk python3 "$t.py" 2>&1); echo "$OUT" | grep -E "✖|ИТОГ|Traceback|Error" | head -20
  echo "$OUT" | grep -q "ИТОГ: всё прошло" || FAIL=1
done
[ $FAIL = 0 ] && echo "ВСЕ ТЕСТЫ ПРОШЛИ" || { echo "ЕСТЬ ОШИБКИ"; exit 1; }
