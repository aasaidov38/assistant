# Архитектура и план развития

## Принципы

1. **Одно ядро, много каналов.** Логика и данные живут только в `core`. Telegram, Алиса, веб, SMS и телефон — это тонкие адаптеры, которые переводят «своё» сообщение в общий формат и отправляют его в ядро.
2. **Каждый сервис в своём контейнере.** Упал или обновляется один, остальные работают.
3. **Данные у вас.** PostgreSQL на вашем VPS, ночные копии, ключи только в `.env`.
4. **ИИ заменяемый.** Провайдер модели и распознавания речи меняется в `.env` без правки кода.
5. **n8n для сценариев, код для логики.** Всё, что удобно собрать мышкой (расписания, уведомления, таблицы, интеграции с сервисами), делается в n8n и общается с ядром по API. Всё, что требует точности и состояния (учёт, разбор чеков, диалоги), остаётся в ядре.
6. **Журнал событий.** Каждое изменение данных пишется в таблицу `events` и уходит в n8n. Новые сценарии подключаются к событиям, не трогая ядро.

## Схема

```
                 ┌────────────── адаптеры каналов ──────────────┐
 Пользователь ──►│ telegram │ alice* │ web* │ sms* │ phone*      │
                 └──────┬───────────────────────────────────────┘
                        │  POST /v1/inbound, /v1/action  (X-API-Key)
                        ▼
               ┌──────────────────────── core ─────────────────────────┐
               │ brain.py      понимает сообщение, выбирает модуль     │
               │ finance.py    деньги       nutrition.py  еда, КБЖУ    │
               │ tasks.py      дела         ticktick.py   TickTick     │
               │ ai.py         ИИ и речь    notify.py     исходящие    │
               │ jobs.py       напоминания, отправка событий           │
               └───────┬───────────────────────────────┬───────────────┘
                       ▼                               ▼
                 db (PostgreSQL)                 n8n (сценарии)
                       │
                 backup (ночные копии)
```

`*` — будущие адаптеры.

## Формат обмена с ядром

Входящее сообщение (любой канал):

```json
POST /v1/inbound
{"channel": "telegram", "external_user_id": "123456789",
 "kind": "text | photo | voice", "text": "...", "file_b64": "...", "mime": "image/jpeg"}
```

Ответ:

```json
{"text": "💾 Записал: ...", "buttons": [[{"text": "❌ Отменить", "action": "t:15:del"}]]}
```

Нажатие кнопки: `POST /v1/action {"channel", "external_user_id", "action"}`.

Один человек может писать из разных каналов: связь хранится в таблице `user_identities` (канал + внешний ID → пользователь).

## Как добавить новый канал (пример: Алиса)

1. Папка `adapters/alice` со своим `Dockerfile` и маленьким веб-сервером, который принимает запросы Яндекс Диалогов.
2. Адаптер переводит запрос в `/v1/inbound` с `channel: "alice"` и возвращает `text` в ответе Алисе (кнопки можно озвучить или показать как suggest).
3. Новый сервис в `docker-compose.yml` в сетях `internal` и `shared`, адрес в Caddyfile (Алисе нужен HTTPS).
4. Связка аккаунта: запись в `user_identities` для `alice` и ID пользователя Яндекса.
5. Для исходящих сообщений (если канал их поддерживает) добавляется функция в `notify.SENDERS`.

## Как добавить новый модуль (пример: показания счётчиков)

1. Миграция `core/migrations/003_meters.sql` с таблицей (следующий свободный номер). Если данные нужны для вопросов к ИИ, добавьте представление в схему `ro` и `GRANT SELECT ... TO assistant_ro`, а описание — в `prompts.SQL_WRITER`.
2. Файл `core/app/meters.py`: `save`, `render_saved`, `handle_action`.
3. Новый intent в `prompts.TEXT_PARSER` и ветка в `brain.handle_text`.
4. Префикс кнопок (например `w:`) в словаре обработчиков `brain.handle_action`.
5. При желании эндпоинт в `main.py` для n8n и веба.

## Модули ИИ (версия 1.1)

| Модуль | Файл | Что делает |
|---|---|---|
| Вопросы к данным | `analytics.py` | ИИ пишет SQL к представлениям `ro.*`; запрос выполняется от роли `assistant_ro` (только чтение, только своя семья, 5 секунд); ИИ пересказывает результат |
| Недельный разбор | `digest.py` | Цифры за 7 дней против прошлой недели + советы ИИ; расписание `WEEKLY_DIGEST_DAY/HOUR` |
| Лимиты | `budgets.py` | Таблицы `budgets`, `budget_alerts`; предупреждения 80% и 100% один раз за месяц, событие `budget.alert` |
| Чеки по позициям | `finance.py` | Таблица `transaction_items` (название, количество, сумма, группа товара), кнопка «Позиции чека» |
| Рецепты | `recipes.py` | Рецепты по запросу или фото продуктов; ответ хранится в `suggestions`, кнопки «купить недостающее» и «съел» |
| Список покупок | `shopping.py` | Таблица `shopping_items`, общий список семьи, вычёркивание по чеку, выгрузка задачей в TickTick |
| Задачи для семьи | `tasks.py` | Поле `tasks.assignee_id`; задача уходит в TickTick исполнителя, уведомление и напоминание ему |

## Версия 1.2

| Модуль | Файл | Что делает |
|---|---|---|
| Веб-панель | `web.py`, `static/index.html` | `/web` в ядре; вход по одноразовой ссылке из бота (`web_login_tokens`), сессия в HttpOnly-cookie (`web_sessions`), API `/web/api/*` только своей семьи, защита от подделки запросов заголовком `X-Requested-With` |
| Алиса | `alice.py` | Вебхук `/alice/<ALICE_SECRET>`; привязка по коду (`link_codes` → `user_identities`); ответ за 2,4 с, иначе досылка в Telegram |
| Утренняя сводка | `morning.py` | Open-Meteo без ключа; время у каждого своё (`users.morning_time`) |
| Привычки | `habits.py` | Таблицы `metrics`, `metric_goals`; представления `ro.metrics`, `ro.metric_goals` |
| Покупки по отделам | `shopping.py` | Поле `shopping_items.dept` |
| Мониторинг | `scripts/monitor.sh` | Хост, cron, напрямую в Telegram API |
| Синхронизация TickTick → ассистент | `ticktick_sync.py` | Раз в 5 минут: новые, изменённые, выполненные и удалённые задачи; досылка закрытия, если не дошло (миграция 004) |

## Версия 1.4: отмена записей

| Модуль | Файл | Что делает |
|---|---|---|
| Отмена и возврат | `undo.py` | Журнал операций семьи (деньги, чеки целиком, еда, показатели, дела, покупки), отмена через `deleted_at` и возврат; дела удаляются вместе с TickTick (без возврата). Ссылки на операции: `t.<id>`, `g.<uuid>`, `m.<id>`, `h.<id>`, `k.<id>`, `s.<id>`; кнопки `u:<ref>:del|back` |
| Команды | `brain.py`, `prompts.py` | Простые фразы («отмени последнюю трату», `/history`) распознаются без ИИ; сложные («удали кофе») — intent `undo` |
| Панель | `web.py`, `static/index.html` | `GET /web/api/history`, `POST /web/api/ops/<ref>/cancel|restore`, вкладка «История», кнопки ✕, подсказка «Вернуть» |
| Миграция | `005_undo.sql` | `metrics.created_at`, индексы по времени создания |

## Версия 1.5: деньги, гараж, дом, здоровье

| Модуль | Файл | Таблицы | Кнопки |
|---|---|---|---|
| Регулярные платежи | `bills.py` | `bills`, `bill_payments` (связь с `transactions`: отмена оплаты откатывает срок) | `b:` |
| Импорт выписок | `bank.py` | `bank_imports`, `bank_rows`; CSV — свой разбор, PDF — `ai.complete_document` (Claude) | `i:` |
| Накопления | `savings.py` | `savings_goals`, `savings_moves` | `v:` |
| Гараж | `garage.py` | `vehicles`, `odometer`, `service_rules`, `service_log`, `season_sent`, `transactions.vehicle_id` | `o:` |
| Документы | `papers.py` | `papers`, `attachments` (фото в bytea) | `d:` |
| Счётчики | `meters.py` | `meters`, `meter_readings`, `meter_reminders` | `r:` |
| Заметки | `notes.py` | `family_notes` | — |
| Лекарства | `meds.py` | `meds`, `med_intakes` | `x:` |
| Панель | `web_life.py` | API `/web/api/bills|bank|savings|garage|papers|files|meters|notes|meds` | — |

Напоминания — в `jobs.loop` (каждые 30 с): `meds.send_due` по минутам, остальные раз в день после `DAILY_REMINDER_HOUR`; получатель — `notify.remind` (`REMINDERS_TO`). Утренняя сводка собирает `morning_lines()` модулей. Для вопросов к данным — представления `ro.bills`, `ro.savings`, `ro.vehicles`, `ro.service_log`, `ro.papers`, `ro.meter_readings`, `ro.meds`, `ro.med_intakes`. Отмена (`undo.py`) знает новые записи: `v.` (накопления), `o.`/`l.` (пробег, обслуживание), `d.` (документы), `r.` (показания), `n.` (заметки).

## Версия 1.6: поездки и новая панель

| Что | Файл | Детали |
|---|---|---|
| Поездки | `trips.py`, миграция `007_trips.sql` | `trips`, `trip_places`, `trip_checklist`, `transactions.trip_id` (проставляется в `finance.save` по датам, кроме `source='bill'`), `ro.trips`, в `ro.transactions` — столбец `trip`; кнопки `j:` |
| Погода по маршруту | `trips.weather` | Open-Meteo forecast + geocoding, без ключа, горизонт 16 дней |
| Напоминания | `trips.send_due`, `trips.morning_lines` | за 3 дня и за 1 день; отметки в `trips.reminded` |
| API панели | `web_life.py` | `GET /web/api/dashboard`, `GET/POST /web/api/trips…`, `…/weather`, `…/items`, `…/toggle`, `…/delete` |
| Дизайн | `static/index.html` | Chart.js 4.4.1 локально; категориальная палитра в фиксированном порядке (свет/тьма), статусные цвета отдельно; без «пончиков» — сегментная полоса; одна ось на графике; `prefers-reduced-motion` отключает анимации; настройки Chart.defaults меняются по свойствам, а не заменой объектов |

## Версия 1.7: долги, настройки, личные и общие разделы

| Что | Файл | Детали |
|---|---|---|
| Миграция | `008_settings_debts.sql` | `users.settings` (jsonb), `users.digest_day/hour`, `families.sections` (jsonb), `outbox`, `debts`, `debt_moves`, функция `ro.can_see(section, owner)`, представления `ro.*` пересозданы с фильтром по разделу, `ro.debts` |
| Разделы | `scope.py` | `SECTIONS` и режимы по умолчанию; `enter(user)` в начале запроса (бот: `brain.handle_*`, панель: `web.current_user`) кладёт в contextvar, кто смотрит; `cond(section, column)` — SQL-условие для списков; `as_user` — для сводок в фоне. Фоновые задачи `enter` не вызывают |
| Настройки | `prefs.py` | `TOPICS`, `TABS`, `GOALS`; `get`, `full`, `update`, `quiet_until` |
| Уведомления | `notify.py` | `send(user, reply, topic, urgent, quiet)`: выключенные темы не шлются, в тихие часы — `outbox` (или `drop` для копий), `deliver` — без проверок, `flush_outbox` в `jobs`; `remind(..., topic, section)` — в личном разделе автору; `remind_about(topic, ...)` |
| Долги | `debts.py` | кнопки `e:`, отмена `e.` (долг) и `p.` (движение), напоминания `send_due` раз в день, `morning_lines` |
| Вопросы к данным | `analytics.py` | дополнительно `set_config('app.user_id')`; `ro.can_see` — SECURITY DEFINER, читает `families.sections` |
| Панель | `web_life.py`, `static/index.html` | `/web/api/debts…`, `/web/api/settings`, `/web/api/sections`; `/web/api/me` отдаёт `ui`, `modes`, `owner`; тема через `html[data-theme]` + `localStorage.ui` до отрисовки |

Важно для следующих миграций: если пересоздаёте `ro.*`, сохраняйте условие `ro.can_see(...)`.

## Версия 1.7.2: сайт проекта

| Что | Файл | Детали |
|---|---|---|
| Сайт | `site.py`, `static/site/` | `GET /` → `index.html`, `GET /site/{path}` → файлы только из `static/site` и только известных типов (jpg, png, webp, svg, css, js, ico); кэш 10 минут / сутки |
| Caddy | `/opt/n8n/Caddyfile` | `@public path … / /site/*` |
| Мелочь | `debts.py`, `web_life.py` | подписи сроков долгов: «Имя: ждём возврат» / «Имя: вернуть» |

## План развития

| Направление | Как сделать | Где |
|---|---|---|
| SMS | Провайдер SMS (SMS.ru, SMSC и т.п.) как отправитель в `notify.SENDERS`; входящие SMS банка → n8n → `POST /v1/transactions` | ядро + n8n |
| Звонки и запись разговоров | Облачная АТС с API и вебхуком о записи → n8n → распознавание → краткий протокол | n8n + ядро |
| Протоколы совещаний и мемо | Аудио в бота → модуль `meetings`: распознавание по частям, резюме ИИ, решения и задачи в TickTick | ядро |
| Коммуникации с людьми | Модуль контактов и истории общения, черновики писем через ИИ | ядро |
| Банковские выписки | Импорт CSV или PDF выписки → разбор → операции | ядро |

## Ресурсы

На VPS 1 CPU / 2 ГБ: ассистент целиком занимает около 250–400 МБ. Тяжёлую работу (распознавание фото и речи) делают облачные сервисы. Локальные модели ИИ на таком сервере работать не будут, для них нужен сервер с видеокартой.

Лимиты памяти заданы в `docker-compose.yml` (`mem_limit`), чтобы один сервис не мог занять всю память и уронить n8n или VPN.

## Версия 1.7.1: чат и Android-приложение
- `mobile.py` + `static/chat.html`: страница `/web/chat` и API `/web/api/chat/send|action`. Сообщение превращается в `Inbound`/`Action` от Telegram-привязки вошедшего пользователя и идёт в `brain` — логика одна для бота, панели и приложения.
- Android-приложение (отдельный архив, пакет `ru.saidov.assistant`) — оболочка над `/web/`: App Links (Caddy отдаёт `/.well-known/assetlinks.json`), «Поделиться» → `/web/chat`, голос OGG/Opus через мост `window.Android`.

## Версия 1.8: обновления, голос, карты, интеграции, push

| Что | Файлы | Детали |
|---|---|---|
| Версия и обновления | `release.py`, `scripts/host-agent.sh`, `VERSION`, `core/CHANGELOG.md`, миграция `009_v18.sql` (`app_state`) | Ядро и агент общаются файлами в `state/` (в ядре `/srv/state`): `host.json` (копии, диск; агент пишет каждую минуту), `update-available.json` (новая версия из ветки `stable`, раз в 30 мин), `update-request.json` (ядро — по кнопке владельца `y:<версия>:go`), `update-result.json` (итог; неудачу ядро сообщает владельцу). Объявление «что нового» — при старте ядра, если версия выросла (`app_state 'announced'`). Предложение — раз в 3 дня (`app_state 'update_offer'`). Ядро не имеет доступа к Docker и git |
| Голос | `static/chat.html`, `ai.audio_name`, `mobile.py` | Удержание кнопки (pointer events, захват указателя), MediaRecorder в браузере, Android-мост в приложении. Тип `audio/webm;codecs=opus` → `audio/webm`, имя файла по типу (`voice.webm`, `.ogg`, `.m4a`) |
| Карты лояльности | `cards.py`, `loyalty_cards`, раздел `cards` (общий), кнопки `c:`, отмена `c.<id>` | Формат: EAN-13/EAN-8 по контрольной цифре, Code 128, QR, текст. Банковские номера (16–19 цифр, 2/4/5/6, алгоритм Луна) отклоняются. Штрихкоды в панели рисуются SVG своим кодом (проверено по таблицам reportlab), QR — `static/qrcode.js` (MIT) |
| Интеграции | `web_life.py` `/web/api/integrations…` | Статусы Telegram/TickTick/Алисы/push по пользователю; отключение TickTick удаляет строку `integrations` |
| PWA и push | `push.py`, `push_subscriptions`, `static/sw.js`, `/web/manifest.webmanifest`, иконки | RFC 8291 (aes128gcm) + VAPID (ES256) на `cryptography`; проверено по примеру из RFC 8291. `notify.send` после Telegram шлёт push (кроме `quiet="drop"` — копий из панели); 404/410 удаляют подписку, 5 ошибок подряд — тоже |
| Тесты | `tests/`, `.github/workflows/tests.yml` | На настоящей PostgreSQL, заглушки asyncpg/fastapi/httpx в `tests/stubs` |

## Версия 1.8.1: навигация и строка ввода

| Что | Файлы | Детали |
|---|---|---|
| Группы разделов | `static/index.html` (`GROUPS`, `#tabs [data-g]`) | 6 групп; `#tabs` — подразделы текущей группы; `localStorage['tab']` и `['subtab']`; ссылки `#cards`, `#card-N`, `#set`, `#import-N`, `#<раздел>` (`goHash`) |
| Строка ввода | `static/chat.html` (режим `embed`), `#dock`/`#chatFrame` в `index.html` | Сообщения рамка → панель: `size{h}`, `open`, `close`, `reply` (обновить раздел), `nav{url}` (кнопки-ссылки из ответов); панель → рамка: `open`, `close`, `focus`. Проверка `origin` и поля `src` |
| Android | `index.html` | `window.onVoiceStarted/onVoice/onVoiceError/onVoicePermission` пересылаются в рамку (мост `window.Android` виден и в рамке). «Поделиться» по-прежнему открывает `/web/chat` |
| Выход | `index.html` | Кнопка в ⚙️ → 👤 Аккаунт, с подтверждением; в шапке её нет |

Ссылку «💬 Чат» из пункта 4 списка «Что нельзя ломать» (HANDOFF 1.7.1) заменяет строка ввода на каждом разделе; адрес `/web/chat` сохранён.
