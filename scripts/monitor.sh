#!/usr/bin/env bash
# monitor.sh — следит за сервером и пишет в Telegram, если что-то сломалось и когда починилось.
#
#   Проверка вручную, отчёт в консоль:   bash /opt/assistant/scripts/monitor.sh --status
#   Пробное сообщение в Telegram:        bash /opt/assistant/scripts/monitor.sh --test
#   Обычный запуск (по cron раз в 5 минут) без параметров: пишет только об изменениях.
#
# Что проверяется:
#   • контейнеры Docker: остановленные (если должны работать), перезапускающиеся, «unhealthy»
#   • ядро ассистента (/health) и адреса из MONITOR_URLS
#   • место на диске, память
#   • свежесть резервных копий: /root/backup-*.tar.gz и ночные копии базы ассистента
#
# Настройки (необязательно) в /opt/assistant/.env:
#   MONITOR_CHAT_ID=123456789          кому писать (по умолчанию первый из ALLOWED_USERS)
#   MONITOR_URLS=https://n8n.saidov.ru/healthz   через пробел, адреса, которые должны отвечать
#   MONITOR_BACKUP_HOURS=26            через сколько часов без нового бэкапа тревожить (0 = не проверять)
#   MONITOR_DISK_PERCENT=85
set -uo pipefail

ENV_FILE="${ENV_FILE:-/opt/assistant/.env}"
STATE_DIR="${STATE_DIR:-/var/lib/assistant-monitor}"
REMIND_HOURS=6                       # напоминать о нерешённой проблеме раз в 6 часов
MODE="${1:-}"

env_get() { grep -m1 "^$1=" "$ENV_FILE" 2>/dev/null | cut -d= -f2- | tr -d '\r' | sed 's/^"//; s/"$//'; }

TOKEN=$(env_get TELEGRAM_BOT_TOKEN)
CHAT=$(env_get MONITOR_CHAT_ID)
[ -z "$CHAT" ] && CHAT=$(env_get ALLOWED_USERS | cut -d, -f1 | cut -d: -f1 | tr -d ' ')
URLS=$(env_get MONITOR_URLS)
BACKUP_H=$(env_get MONITOR_BACKUP_HOURS); BACKUP_H=${BACKUP_H:-26}
DISK_MAX=$(env_get MONITOR_DISK_PERCENT); DISK_MAX=${DISK_MAX:-85}
HOST=$(hostname)

send() {
  if [ -z "$TOKEN" ] || [ -z "$CHAT" ]; then
    echo "Не могу отправить в Telegram: нет TELEGRAM_BOT_TOKEN или получателя в $ENV_FILE" >&2
    return 1
  fi
  curl -s -m 20 -o /dev/null -w '%{http_code}' "https://api.telegram.org/bot$TOKEN/sendMessage" \
    --data-urlencode "chat_id=$CHAT" --data-urlencode "text=$1" -d parse_mode=HTML \
    -d disable_web_page_preview=true | grep -q '^200$'
}

PROBLEMS=()
problem() { PROBLEMS+=("$(printf '%s' "$1" | sed 's/&/\&amp;/g; s/</\&lt;/g; s/>/\&gt;/g')"); }

# ---------- Контейнеры ----------
if command -v docker >/dev/null 2>&1; then
  while IFS='|' read -r name status policy health; do
    [ -z "$name" ] && continue
    case "$status" in
      running)
        [ "$health" = "unhealthy" ] && problem "🟠 Контейнер $name: unhealthy (не отвечает на проверку)";;
      restarting)
        problem "🔴 Контейнер $name постоянно перезапускается";;
      exited|dead)
        # Тревожим только о тех, что должны работать всегда
        case "$policy" in always|unless-stopped|on-failure) problem "🔴 Контейнер $name остановлен ($status)";; esac;;
    esac
  done < <(docker ps -a --format '{{.Names}}' | while read -r c; do
             docker inspect --format '{{.Name}}|{{.State.Status}}|{{.HostConfig.RestartPolicy.Name}}|{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$c" 2>/dev/null | sed 's#^/##'
           done)
else
  problem "🔴 Docker не найден"
fi

# ---------- Адреса ----------
code=$(curl -s -m 10 -o /dev/null -w '%{http_code}' http://127.0.0.1:8100/health || true)
[ "$code" = "200" ] || problem "🔴 Ядро ассистента не отвечает (код ${code:-нет ответа})"
for u in $URLS; do
  code=$(curl -s -m 15 -o /dev/null -w '%{http_code}' "$u" || true)
  case "$code" in 2*|3*) ;; *) problem "🔴 $u не отвечает (код ${code:-нет ответа})";; esac
done

# ---------- Диск и память ----------
disk=$(df -P / | awk 'NR==2{gsub("%","",$5); print $5}')
[ "${disk:-0}" -ge "$DISK_MAX" ] && problem "🟠 Диск заполнен на ${disk}% (свободно $(df -h / | awk 'NR==2{print $4}'))"
avail=$(awk '/MemAvailable/{print int($2/1024)}' /proc/meminfo)
swap_t=$(awk '/SwapTotal/{print int($2/1024)}' /proc/meminfo); swap_f=$(awk '/SwapFree/{print int($2/1024)}' /proc/meminfo)
if [ "${avail:-999}" -lt 100 ] && [ "$swap_t" -gt 0 ] && [ $(( (swap_t - swap_f) * 100 / swap_t )) -ge 80 ]; then
  problem "🟠 Мало памяти: свободно ${avail} МБ, подкачка занята на $(( (swap_t - swap_f) * 100 / swap_t ))%"
fi

# ---------- Резервные копии ----------
age_h() { echo $(( ( $(date +%s) - $(stat -c %Y "$1") ) / 3600 )); }
if [ "$BACKUP_H" -gt 0 ]; then
  last=$(ls -1t /root/backup-*.tar.gz 2>/dev/null | head -1)
  if [ -z "$last" ]; then
    problem "🟠 Нет ни одной резервной копии сервера (/root/backup-*.tar.gz)"
  elif [ "$(age_h "$last")" -ge "$BACKUP_H" ]; then
    problem "🟠 Резервная копия сервера устарела: последняя $(age_h "$last") ч назад ($(basename "$last"))"
  elif [ "$(stat -c %s "$last")" -lt 100000 ]; then
    problem "🟠 Последняя резервная копия подозрительно маленькая: $(du -h "$last" | cut -f1)"
  fi
  dbl=$(ls -1t /opt/assistant/backups/daily/*.sql.gz 2>/dev/null | head -1)
  if [ -n "$dbl" ] && [ "$(age_h "$dbl")" -ge "$BACKUP_H" ]; then
    problem "🟠 Ночная копия базы ассистента устарела: $(age_h "$dbl") ч назад"
  fi
fi

# ---------- Отчёт ----------
report() {
  if [ ${#PROBLEMS[@]} -eq 0 ]; then
    echo "✅ $HOST: всё в порядке"
  else
    echo "⚠️ $HOST: проблем ${#PROBLEMS[@]}"; printf '%s\n' "${PROBLEMS[@]}"
  fi
  echo "Диск: ${disk}% · свободно памяти: ${avail} МБ · подкачка занята: $(( swap_t > 0 ? (swap_t - swap_f) * 100 / swap_t : 0 ))%"
}

if [ "$MODE" = "--status" ]; then report; exit 0; fi
if [ "$MODE" = "--test" ]; then
  if send "🧪 Проверка мониторинга
$(report)"; then echo "Сообщение отправлено"; else echo "Не удалось отправить"; exit 1; fi
  exit 0
fi

# Обычный запуск: сравниваем с прошлым состоянием и пишем только об изменениях
mkdir -p "$STATE_DIR"
NOW_F="$STATE_DIR/now.txt"; OLD_F="$STATE_DIR/last.txt"; SENT_F="$STATE_DIR/last_alert"
printf '%s\n' "${PROBLEMS[@]}" | sed '/^$/d' | sort > "$NOW_F"
touch "$OLD_F"
new=$(comm -13 "$OLD_F" "$NOW_F")
fixed=$(comm -23 "$OLD_F" "$NOW_F")
msg=""
[ -n "$new" ] && msg+="⚠️ <b>$HOST: проблема</b>
$new
"
[ -n "$fixed" ] && msg+="✅ <b>Восстановлено</b>
$fixed
"
# Напоминание, если проблема висит долго
if [ -z "$msg" ] && [ -s "$NOW_F" ] && [ -f "$SENT_F" ] && [ $(( $(date +%s) - $(stat -c %Y "$SENT_F") )) -ge $(( REMIND_HOURS * 3600 )) ]; then
  msg="⏰ <b>$HOST: всё ещё не решено</b>
$(cat "$NOW_F")"
fi
if [ -n "$msg" ]; then
  if send "$msg"; then
    mv "$NOW_F" "$OLD_F"; touch "$SENT_F"
  fi   # не отправилось — попробуем в следующий раз, состояние не меняем
else
  mv "$NOW_F" "$OLD_F"
fi
exit 0
