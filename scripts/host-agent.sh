#!/usr/bin/env bash
# Агент ассистента на сервере (с версии 1.8). Запускается cron'ом раз в минуту от root:
#   * * * * * /opt/assistant/scripts/host-agent.sh >> /var/log/assistant-agent.log 2>&1
#
# Что делает:
#   1. state/host.json — когда была последняя копия сервера и копия в Яндексе, место на диске.
#   2. Раз в 30 минут смотрит ветку stable в GitHub. Если там версия новее — пишет
#      state/update-available.json (версия и «что нового»), ядро предложит владельцу обновиться.
#   3. Если ядро положило state/update-request.json (владелец нажал «Обновить») — обновляет:
#      копия базы → новый код → пересборка → проверка. Не поднялось — возвращает прежний код.
#      Итог пишет в state/update-result.json.
# Секретов не читает и не выводит. Файлы .env, backups/ и state/ git не трогает (они в .gitignore).
set -uo pipefail
cd "$(dirname "$0")/.."
ROOT=$(pwd)
ST="$ROOT/state"
BRANCH=${ASSISTANT_BRANCH:-stable}
mkdir -p "$ST"
chown 10001 "$ST" 2>/dev/null || true      # ядро в контейнере работает от пользователя 10001
exec 9>"$ST/.agent.lock"
flock -n 9 || exit 0                        # предыдущий запуск ещё работает (например, обновление)

log() { echo "$(date '+%F %T') $*"; }
now=$(date +%s)
current=$(cat VERSION 2>/dev/null || echo 0)

# ---------- 1. Состояние сервера ----------
python3 - "$ST" "$current" <<'PY'
import glob, json, os, re, shutil, sys, datetime as dt
st, current = sys.argv[1], sys.argv[2]
utc = lambda t: dt.datetime.fromtimestamp(t, dt.timezone.utc).isoformat()
out = {"at": dt.datetime.now(dt.timezone.utc).isoformat(), "version": current}
files = sorted(glob.glob("/root/backup-*.tar.gz"), key=os.path.getmtime)
if files:
    f = files[-1]
    out["local_backup"] = {"file": os.path.basename(f), "at": utc(os.path.getmtime(f)),
                           "size_mb": round(os.path.getsize(f) / 1048576, 1)}
log = "/var/log/offsite-backup.log"
if os.path.exists(log):
    ok = err = None
    with open(log, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = re.match(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) (.*)", line)
            if not m:
                continue
            if m.group(2).strip() == "Готово":
                ok = m.group(1)
            elif "Ошибка" in m.group(2) or "error" in m.group(2).lower():
                err = m.group(1)
    loc = lambda s: dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S").astimezone().isoformat() if s else None
    out["offsite"] = {"ok_at": loc(ok), "error_at": loc(err)}
du = shutil.disk_usage("/")
out["disk"] = {"free_gb": round(du.free / 1073741824, 1), "used_pct": round(du.used * 100 / du.total)}
tmp = os.path.join(st, "host.json.tmp")
with open(tmp, "w", encoding="utf-8") as fh:
    json.dump(out, fh, ensure_ascii=False)
os.replace(tmp, os.path.join(st, "host.json"))
PY

# ---------- 2. Есть ли новая версия ----------
newer() {  # newer A B → 0, если версия A новее B
  python3 -c "import re,sys; v=lambda s: tuple(int(x) for x in re.findall(r'\d+', s)[:4]); sys.exit(0 if v(sys.argv[1]) > v(sys.argv[2]) else 1)" "$1" "$2"
}
last_fetch=$(stat -c %Y "$ST/.last_fetch" 2>/dev/null || echo 0)
if [ -d .git ] && [ $((now - last_fetch)) -ge 1800 ]; then
  touch "$ST/.last_fetch"
  if timeout 120 git fetch -q origin "$BRANCH" 2>/dev/null; then
    latest=$(git show "origin/$BRANCH:VERSION" 2>/dev/null | tr -d '[:space:]')
    if [ -n "$latest" ] && newer "$latest" "$current"; then
      git show "origin/$BRANCH:core/CHANGELOG.md" > "$ST/.changelog" 2>/dev/null || : > "$ST/.changelog"
      python3 - "$ST" "$latest" "$current" <<'PY'
import json, os, re, sys, datetime as dt
st, ver, cur = sys.argv[1:4]
text = open(os.path.join(st, ".changelog"), encoding="utf-8", errors="replace").read()
v = lambda s: tuple(int(x) for x in re.findall(r"\d+", s)[:4])
notes, inside = [], False
for line in text.splitlines():
    if line.startswith("## "):
        if inside:
            break
        parts = line[3:].split()
        inside = bool(parts) and v(parts[0]) == v(ver)
    elif inside and line.lstrip().startswith(("- ", "* ")):
        notes.append(line.lstrip()[2:].strip())
data = {"version": ver, "current": cur, "notes": notes[:8], "at": dt.datetime.now(dt.timezone.utc).isoformat()}
tmp = os.path.join(st, "update-available.json.tmp")
with open(tmp, "w", encoding="utf-8") as fh:
    json.dump(data, fh, ensure_ascii=False)
os.replace(tmp, os.path.join(st, "update-available.json"))
PY
      log "Доступна версия $latest (сейчас $current)"
    else
      rm -f "$ST/update-available.json"
    fi
  else
    log "Не получилось проверить GitHub (git fetch)"
  fi
fi

# ---------- 3. Обновление по кнопке ----------
[ -f "$ST/update-request.json" ] || exit 0
mv "$ST/update-request.json" "$ST/update-request.processing"
want=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('version',''))" "$ST/update-request.processing" 2>/dev/null)
result() {  # result ok|fail "сообщение"
  python3 - "$ST" "$want" "$current" "$1" "$2" <<'PY'
import json, os, sys, datetime as dt
st, ver, cur, ok, msg = sys.argv[1:6]
data = {"version": ver, "from": cur, "ok": ok == "ok", "message": msg, "at": dt.datetime.now(dt.timezone.utc).isoformat()}
with open(os.path.join(st, "update-result.json"), "w", encoding="utf-8") as fh:
    json.dump(data, fh, ensure_ascii=False)
PY
  rm -f "$ST/update-request.processing"
}
if [ ! -d .git ]; then result fail "Папка /opt/assistant не подключена к GitHub — обновите через Claude Code."; exit 0; fi
if ! [[ "$want" =~ ^[0-9]+(\.[0-9]+){0,3}$ ]]; then result fail "Непонятная версия в запросе: $want"; exit 0; fi
log "Обновление $current → $want"
prev=$(git rev-parse HEAD)
timeout 120 git fetch -q origin "$BRANCH" || { result fail "Не получилось скачать код с GitHub."; exit 0; }
target=$(git rev-parse "origin/$BRANCH")
if [ "$(git show "$target:VERSION" | tr -d '[:space:]')" != "$want" ]; then
  result fail "В ветке $BRANCH уже другая версия, чем предлагалось. Проверьте /version."; exit 0
fi
echo "== копия базы"; bash scripts/backup-now.sh || { result fail "Не получилось сделать копию базы — обновление не начинал."; exit 0; }
git checkout -q --force "$target" || { result fail "git checkout не сработал."; exit 0; }
chmod +x scripts/*.sh 2>/dev/null || true
up_ok() {   # ждём до 3 минут, пока ядро ответит нужной версией
  for _ in $(seq 1 36); do
    sleep 5
    v=$(curl -s -m 4 http://127.0.0.1:8100/openapi.json | python3 -c "import json,sys; print(json.load(sys.stdin)['info']['version'])" 2>/dev/null)
    [ "$v" = "$1" ] && curl -sf -m 4 http://127.0.0.1:8100/health >/dev/null && return 0
  done
  return 1
}
if docker compose up -d --build >/tmp/assistant-build.log 2>&1 && up_ok "$want"; then
  docker image prune -f >/dev/null 2>&1
  log "Обновлено до $want"
  result ok "Обновлено до $want"
  exit 0
fi
reason=$( (tail -n 5 /tmp/assistant-build.log; docker compose logs --tail 15 core 2>&1) | grep -viE "token|key|password|secret" | tail -n 12)
log "Новая версия не поднялась, возвращаю $current"
git checkout -q --force "$prev"
docker compose up -d --build >/dev/null 2>&1
up_ok "$current" && back="прежняя версия работает" || back="ВНИМАНИЕ: прежняя версия тоже не ответила, нужна проверка"
result fail "Новая версия не запустилась, $back. Последние строки журнала: $reason"
