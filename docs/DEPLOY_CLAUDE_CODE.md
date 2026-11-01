# Обновление ассистента через Claude Code

Что нужно заранее:
- вход на сервер по SSH-ключу настроен, команда `ssh assistant "hostname"` работает без пароля;
- `assistant.zip` с новой версией лежит в папке на компьютере (например, «Загрузки»).

Откройте Claude Code в этой папке и вставьте задание ниже целиком.

---

```
Задача: обновить моего семейного ассистента на сервере до новой версии из файла assistant.zip в текущей папке.

Сервер: ssh-алиас `assistant` (root, Ubuntu 24.04). Ассистент лежит в /opt/assistant (Docker Compose:
core, telegram, db, backup). Рядом работают n8n и Caddy в /opt/n8n — их НЕ трогать.

Правила:
- Не выводи на экран содержимое /opt/assistant/.env и любые ключи, токены, пароли. Можно проверять только
  наличие строк через grep -c.
- Ничего не удаляй на сервере. Перед изменениями делай копии, как описано ниже.
- Перед каждой командой, которая что-то меняет на сервере, коротко скажи, что она делает.
- Если шаг завершился ошибкой — остановись, покажи вывод (без секретов) и предложи, что делать. Не импровизируй
  с откатом базы данных без моего согласия.

Шаги:
1. Проверь доступ: ssh assistant "hostname && docker compose -f /opt/assistant/docker-compose.yml ps --format '{{.Name}} {{.Status}}'"
2. Резервные копии:
   ssh assistant "set -e; D=\$(date +%F_%H%M); cp /opt/assistant/.env /root/assistant.env.bak-\$D;
     tar czf /root/assistant-code-\$D.tar.gz -C /opt --exclude=assistant/backups assistant;
     cd /opt/assistant && bash scripts/backup-now.sh; ls -lh /root/assistant-code-\$D.tar.gz"
3. Загрузи архив: scp assistant.zip assistant:/opt/assistant.zip
4. Новые настройки (добавить, только если их ещё нет):
   ssh assistant "cd /opt/assistant; grep -q '^REMINDERS_TO=' .env || echo 'REMINDERS_TO=owner' >> .env;
     grep -q '^DAILY_REMINDER_HOUR=' .env || echo 'DAILY_REMINDER_HOUR=10' >> .env; grep -c '^REMINDERS_TO=' .env"
5. Распакуй и обнови:
   ssh assistant "cd /opt && unzip -o assistant.zip && cd /opt/assistant && bash scripts/update.sh"
6. Проверь:
   - ssh assistant "cd /opt/assistant && docker compose logs --tail 60 core | grep -E 'миграц|Ядро запущено|Error|Traceback'"
     Ожидается «Применяю миграцию 008_settings_debts.sql» (и более ранние, если их ещё не было) и «Ядро запущено», без Traceback.
   - ssh assistant "curl -s http://127.0.0.1:8100/openapi.json | grep -o '\"version\":\"[^\"]*\"' | head -1"  → 1.7.2
   - ssh assistant "cd /opt/assistant && bash scripts/check.sh"
   - ssh assistant "curl -s -o /dev/null -w '%{http_code}' https://assistant.saidov.ru/health"  → 200
6a. Android-приложение (с версии 1.7.1): ссылки https://assistant.saidov.ru/web… должны открываться сразу
   в приложении. Для этого Caddy отдаёт файл /.well-known/assetlinks.json. Сделай один раз:
   - проверь, нет ли уже: ssh assistant "grep -c assetlinks /opt/n8n/Caddyfile"  (1 и больше — пропусти этот шаг);
   - копия: ssh assistant "cp /opt/n8n/Caddyfile /root/Caddyfile.bak-\$(date +%F_%H%M)";
   - вставь в /opt/n8n/Caddyfile строки из файла docs/Caddyfile-assetlinks.txt (без строк-комментариев #)
     первой строкой внутри блока `assistant.saidov.ru {`. Остальное в файле не меняй;
   - проверка и применение (имя сервиса Caddy уточни: cd /opt/n8n && docker compose ps --format '{{.Service}}'):
     ssh assistant "cd /opt/n8n && docker compose exec -T caddy caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile && docker compose exec -T caddy caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile"
     Если validate ругается — верни копию Caddyfile и сообщи мне.
   - ssh assistant "curl -s https://assistant.saidov.ru/.well-known/assetlinks.json"  → JSON с ru.saidov.assistant
   - ssh assistant "curl -s -o /dev/null -w '%{http_code}' https://assistant.saidov.ru/web/chat"  → 303
6b. Сайт проекта (с версии 1.7.2): страница https://assistant.saidov.ru/ и её картинки /site/… Ядро их уже отдаёт,
   осталось открыть эти два пути в Caddy. Сделай один раз:
   - проверь, нет ли уже: ssh assistant "grep -n '@public' /opt/n8n/Caddyfile"
     Если в строке блока assistant.saidov.ru уже есть « / /site/*» — пропусти этот шаг;
   - копия: ssh assistant "cp /opt/n8n/Caddyfile /root/Caddyfile.bak-\$(date +%F_%H%M)";
   - в строке `@public path /ticktick/* /health /web /web/* /alice/*` (она внутри блока `assistant.saidov.ru {`)
     добавь в конец « / /site/*». Получится: `@public path /ticktick/* /health /web /web/* /alice/* / /site/*`.
     Больше в файле ничего не меняй (блок assetlinks и `respond 404` оставь как есть);
   - проверка и применение — так же, как в 6a (caddy validate, затем caddy reload). Если validate ругается — верни копию;
   - проверь снаружи:
     ssh assistant "curl -s https://assistant.saidov.ru/ | grep -c 'Семейный ассистент'"  → 1 и больше
     ssh assistant "curl -s -o /dev/null -w '%{http_code}' https://assistant.saidov.ru/site/img/home.jpg"  → 200
     ssh assistant "curl -s -o /dev/null -w '%{http_code}' https://assistant.saidov.ru/web/"  → 200 или 401 (как раньше)
     ssh assistant "curl -s -o /dev/null -w '%{http_code}' https://assistant.saidov.ru/v1/users"  → 404 (API закрыто)
     ssh assistant "curl -s https://assistant.saidov.ru/.well-known/assetlinks.json | grep -c ru.saidov.assistant"  → 1
7. Итог: кратко напиши, что обновлено, какие миграции применились, и есть ли ошибки в логах.
   Напомни мне написать боту /start, чтобы обновилось меню команд.

Если ядро не запустилось после шага 5: покажи последние 80 строк логов core (без секретов) и предложи откат кода:
  ssh assistant "cd /opt && rm -rf /opt/assistant.failed && mv assistant assistant.failed &&
    tar xzf /root/assistant-code-ДАТА.tar.gz -C /opt && cp -a assistant.failed/backups assistant/ 2>/dev/null;
    cd /opt/assistant && docker compose up -d --build"
Откат выполняй только после моего «да». База данных при откате не трогается: новые таблицы старому коду не мешают.
```
