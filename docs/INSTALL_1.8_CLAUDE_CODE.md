# Установка версии 1.8 через Claude Code (один раз)

Эта установка особенная: папка ассистента на сервере подключается к GitHub, и ставится **агент обновлений**.
После неё следующие версии ставятся кнопкой «🔄 Обновить» в боте — без архивов и без Claude Code.

Что нужно заранее:
- вход на сервер по SSH-ключу настроен, команда `ssh assistant "hostname"` работает без пароля;
- на сервере стоит версия 1.7.x;
- вы можете зайти на github.com в аккаунт `aasaidov38` (понадобится один раз добавить ключ сервера).

Откройте Claude Code в любой папке и вставьте задание ниже целиком. Где-то посередине Claude попросит вас
добавить ключ на GitHub — это делается мышкой на сайте, инструкция будет в его сообщении.

---

```
Задача: перевести моего семейного ассистента на сервере на обновления из GitHub и установить версию 1.8.

Сервер: ssh-алиас `assistant` (root, Ubuntu 24.04). Ассистент лежит в /opt/assistant (Docker Compose:
core, telegram, db, backup). Рядом работают n8n и Caddy в /opt/n8n — их НЕ трогать.
Код: закрытый репозиторий GitHub aasaidov38/assistant, ветка stable.

Правила:
- Не выводи на экран содержимое /opt/assistant/.env и любые ключи, токены, пароли. Можно проверять только
  наличие строк через grep -c. ЗАКРЫТЫЙ ключ /root/.ssh/assistant_deploy никогда не показывай и не копируй
  с сервера. Показывать можно только открытый ключ (.pub).
- Ничего не удаляй на сервере. Перед изменениями делай копии, как описано ниже.
- Перед каждой командой, которая что-то меняет на сервере, коротко скажи, что она делает.
- Если шаг завершился ошибкой — остановись, покажи вывод (без секретов) и предложи, что делать. Не импровизируй
  с откатом базы данных без моего согласия.

Шаги:
1. Проверь доступ и текущую версию:
   ssh assistant "hostname; docker compose -f /opt/assistant/docker-compose.yml ps --format '{{.Name}} {{.Status}}';
     curl -s http://127.0.0.1:8100/openapi.json | grep -o '\"version\":\"[^\"]*\"' | head -1;
     git --version; python3 --version; which flock curl"
   Ожидается версия 1.7.x. Если git не установлен: ssh assistant "apt-get install -y git".

2. Резервные копии:
   ssh assistant "set -e; D=\$(date +%F_%H%M); cp /opt/assistant/.env /root/assistant.env.bak-\$D;
     tar czf /root/assistant-code-\$D.tar.gz -C /opt --exclude=assistant/backups assistant;
     cd /opt/assistant && bash scripts/backup-now.sh; ls -lh /root/assistant-code-\$D.tar.gz"
   Запомни имя архива — он нужен для отката.

3. Ключ сервера для GitHub (только чтение):
   ssh assistant "test -f /root/.ssh/assistant_deploy || ssh-keygen -q -t ed25519 -N '' -C 'assistant-server' -f /root/.ssh/assistant_deploy;
     cat /root/.ssh/assistant_deploy.pub"
   Покажи мне ОТКРЫТЫЙ ключ (строка ssh-ed25519 …) и попроси сделать на github.com:
     репозиторий aasaidov38/assistant → Settings → Deploy keys → Add deploy key;
     Title: server; Key: вставить строку целиком; галочку «Allow write access» НЕ ставить; Add key.
   Жди моего ответа «готово». Дальше не продолжай без него.

4. Настрой доступ к GitHub с сервера:
   - отпечаток GitHub (сверь, прежде чем доверять):
     ssh assistant "ssh-keyscan -t ed25519 github.com 2>/dev/null | ssh-keygen -lf -"
     Должно быть SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU. Если не совпадает — остановись и сообщи мне.
   - ssh assistant "grep -q '^github.com ssh-ed25519' /root/.ssh/known_hosts 2>/dev/null || ssh-keyscan -t ed25519 github.com >> /root/.ssh/known_hosts;
       grep -q 'Host github-assistant' /root/.ssh/config 2>/dev/null || printf '\nHost github-assistant\n  HostName github.com\n  User git\n  IdentityFile /root/.ssh/assistant_deploy\n  IdentitiesOnly yes\n' >> /root/.ssh/config;
       chmod 600 /root/.ssh/config"
   - проверка: ssh assistant "ssh -T github-assistant 2>&1 | head -1"
     Ожидается «Hi aasaidov38/assistant! You've successfully authenticated…». «Permission denied» — ключ не добавлен
     в Deploy keys, попроси меня проверить шаг 3.

5. Подключи папку к GitHub и получи код 1.8. Файлы .env, backups/ и state/ git не трогает (они в .gitignore),
   остальные файлы кода заменятся версией 1.8:
   ssh assistant "set -e; cd /opt/assistant; test -d .git || git init -q;
     git remote get-url origin >/dev/null 2>&1 || git remote add origin github-assistant:aasaidov38/assistant.git;
     git fetch -q origin stable; git checkout -q -f -B stable origin/stable;
     git log --oneline -1; cat VERSION; ls -la .env | cut -c1-10; mkdir -p state; chown 10001 state; chmod +x scripts/*.sh"
   Ожидается VERSION 1.8 и что .env на месте.

6. Пересобери и запусти (скрипт сам ещё раз сделает копию базы):
   ssh assistant "cd /opt/assistant && bash scripts/update.sh"

7. Проверь:
   - ssh assistant "cd /opt/assistant && docker compose logs --tail 80 core | grep -E 'миграц|Ядро запущено|Error|Traceback'"
     Ожидается «Применяю миграцию 009_v18.sql» и «Ядро запущено», без Traceback.
   - ssh assistant "curl -s http://127.0.0.1:8100/openapi.json | grep -o '\"version\":\"[^\"]*\"' | head -1"  → 1.8
   - ssh assistant "cd /opt/assistant && bash scripts/check.sh"
   - ssh assistant "for p in health web/manifest.webmanifest web/sw.js web/static/icon-192.png web/static/qrcode.js; do
       echo \$p \$(curl -s -o /dev/null -w '%{http_code}' https://assistant.saidov.ru/\$p); done"  → все 200
   - ssh assistant "curl -s -o /dev/null -w '%{http_code}' https://assistant.saidov.ru/v1/users"  → 404 (API закрыто)
   - ssh assistant "docker compose -f /opt/assistant/docker-compose.yml exec -T core python -c 'import cryptography; print(cryptography.__version__)'"
     → номер версии (нужно для push-уведомлений).

8. Поставь агент обновлений (cron раз в минуту) и ротацию его журнала:
   ssh assistant "printf '# Агент семейного ассистента: состояние копий и обновления по кнопке (1.8)\n* * * * * root /opt/assistant/scripts/host-agent.sh >> /var/log/assistant-agent.log 2>&1\n' > /etc/cron.d/assistant-agent;
     chmod 644 /etc/cron.d/assistant-agent;
     printf '/var/log/assistant-agent.log {\n  weekly\n  rotate 4\n  compress\n  missingok\n  notifempty\n}\n' > /etc/logrotate.d/assistant-agent"
   Подожди 2 минуты и проверь:
   ssh assistant "tail -5 /var/log/assistant-agent.log; cat /opt/assistant/state/host.json; echo; ls -la /opt/assistant/state"
   В host.json должны быть at, version 1.8, local_backup (если в /root есть архивы backup-*.tar.gz), disk.
   В файле нет секретов, его можно показывать.

9. Итог: кратко напиши, что сделано, какая версия работает, есть ли ошибки в логах.
   Напомни мне:
   - написать боту /start (обновится меню команд) и /version (покажет версию и резервные копии);
   - если копия в Яндекс Облаке ещё не настроена — /version покажет «не настроено», это отдельная задача
     OFFSITE_BACKUP_CLAUDE_CODE.md.

Если ядро не запустилось после шага 6: покажи последние 80 строк логов core (без секретов) и предложи откат кода:
  ssh assistant "cd /opt && mv assistant assistant.failed-\$(date +%F_%H%M) &&
    tar xzf /root/assistant-code-ДАТА.tar.gz -C /opt && cp -a assistant.failed-*/backups assistant/ 2>/dev/null;
    cd /opt/assistant && docker compose up -d --build"
Откат выполняй только после моего «да». База данных при откате не трогается: новые таблицы старому коду не мешают.
Агент (шаг 8) на старой версии без папки .git ничего не делает, его можно оставить.
```

---

## Что изменится для вас

- В боте: /version, /cards, кнопки «🔄 Обновить сейчас» / «⏰ Напомнить позже», когда выйдет новая версия.
- В панели: вкладка 💳 Карты, в ⚙️ Настройках — 🔌 Интеграции и 🛡 Версия и резервные копии.
- Голосовые удержанием кнопки — в чате панели и в браузере.
- Панель можно установить как приложение и включить push-уведомления (🔌 Интеграции).

Следующие версии: придёт сообщение «🆕 Вышла новая версия» → кнопка «Обновить сейчас». Всё.
