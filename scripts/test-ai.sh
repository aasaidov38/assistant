#!/usr/bin/env bash
# Проверка ИИ и голоса по настройкам из .env (ключи на экран не выводятся):
#   bash scripts/test-ai.sh
cd "$(dirname "$0")/.."
[ -f .env ] || { echo "Нет файла .env"; exit 1; }
get() { grep "^$1=" .env | head -1 | cut -d= -f2- | tr -d '\r'; }

PROV=$(get LLM_PROVIDER); KEY=$(get LLM_API_KEY); URL=$(get LLM_BASE_URL)
echo "Провайдер: ${PROV:-anthropic} | адрес: ${URL:-по умолчанию}"
echo "Длина ключа ИИ: ${#KEY} (0 = ключ не задан)"
case "$KEY" in *[!\ -~]*) echo "⚠ В ключе есть русские буквы или лишние символы: вставьте настоящий ключ";; esac
echo

test_model() {
  local M="$1" LABEL="$2" CODE
  if [ -z "$M" ]; then printf "%-10s не задана\n" "$LABEL"; return; fi
  if [ "${PROV:-anthropic}" = "anthropic" ]; then
    CODE=$(curl -s -o /tmp/ai-test.json -m 60 -w "%{http_code}" "${URL:-https://api.anthropic.com}/v1/messages" \
      -H "x-api-key: $KEY" -H "anthropic-version: 2023-06-01" -H "content-type: application/json" \
      -d "{\"model\":\"$M\",\"max_tokens\":20,\"messages\":[{\"role\":\"user\",\"content\":\"ок\"}]}")
  else
    CODE=$(curl -s -o /tmp/ai-test.json -m 60 -w "%{http_code}" "$URL/chat/completions" \
      -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
      -d "{\"model\":\"$M\",\"messages\":[{\"role\":\"user\",\"content\":\"ок\"}]}")
  fi
  printf "%-10s %-30s HTTP %s\n" "$LABEL" "$M" "$CODE"
  if [ "$CODE" != "200" ]; then
    printf "           "; head -c 220 /tmp/ai-test.json 2>/dev/null | tr '\n' ' '; echo
    case "$CODE" in
      404) echo "           → такого имени нет или модель закрыта для вашего аккаунта: возьмите другую";;
      429) echo "           → исчерпана квота: подождите или возьмите другую модель";;
      503) echo "           → модель перегружена: возьмите другую или подождите";;
      401|400|403) echo "           → проблема с ключом";;
      000) echo "           → нет ответа (сеть или таймаут)";;
    esac
  fi
}
test_model "$(get LLM_MODEL)" "Основная"
test_model "$(get LLM_FALLBACK_MODEL)" "Запасная"
rm -f /tmp/ai-test.json

echo
STT=$(get STT_API_KEY); STT_URL=$(get STT_BASE_URL)
if [ -z "$STT" ]; then
  echo "Голос:     ключ STT_API_KEY не задан (голосовые не работают)"
else
  CODE=$(curl -s -o /dev/null -m 30 -w "%{http_code}" "${STT_URL:-https://api.groq.com/openai/v1}/models" -H "Authorization: Bearer $STT")
  printf "Голос:     ключ Groq HTTP %s" "$CODE"; [ "$CODE" = "200" ] && echo " (всё в порядке)" || echo " (ключ неверный или не вставился)"
fi
