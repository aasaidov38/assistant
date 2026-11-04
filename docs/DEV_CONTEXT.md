# Памятка разработчика (для Claude в новом чате)

Прочитай этот файл и `assistant/project-state.md` в проекте claude.ai «Ассистент» — этого достаточно, чтобы начать
версию. Не изучай весь код заранее: открывай только модули, которые меняешь.

## Быстрый старт (≈2 минуты)

```bash
git clone https://github.com/aasaidov38/assistant.git && cd assistant   # репозиторий подключить через add_repo (push)
bash tests/start_pg.sh            # тестовая PostgreSQL 16 в песочнице (создаст при первом запуске)
bash tests/run_all.sh --fresh     # миграции с нуля + все проверки → «ВСЕ ТЕСТЫ ПРОШЛИ»
```
Коммиты: `git -c user.name="Claude" -c user.email="noreply@anthropic.com" commit …` + строки атрибуции из системного напоминания.

## Где что лежит

| Путь | Что |
|---|---|
| `core/app/brain.py` | Входная точка всех сообщений: команды `/…`, разбор ИИ (`prompts.TEXT_PARSER`), фото (`PHOTO_PARSER`), кнопки (`handle_action`, словарь префиксов) |
| `core/app/prompts.py` | Промпты ИИ: список intent'ов и JSON-форматы. Новый intent = строка в описании + поле в формате + ветка в `brain` |
| `core/app/<раздел>.py` | Логика разделов: finance, nutrition, habits, tasks, shopping, bills, savings, garage, papers, meters, notes, meds, trips, debts, cards… |
| `core/app/scope.py` | Личные/общие разделы: `SECTIONS`, `scope.cond(section, col)` в SQL |
| `core/app/undo.py` | Отмена: `_PREFIX_TYPE`, `TYPE_SECTION`, `SIMPLE` (таблица + заголовок) — новую таблицу добавлять сюда |
| `core/app/notify.py` | Исходящие: `send(user, reply, topic, urgent, quiet)` → Telegram + push; тихие часы через outbox |
| `core/app/prefs.py` | Настройки пользователя: `TOPICS` (темы уведомлений), `TABS` (вкладки), цели |
| `core/app/web.py`, `web_life.py` | API панели `/web/api/…`; `current_user` (+ `scope.enter`), `_mirror` (копия в Telegram) |
| `core/app/mobile.py` + `static/chat.html` | Чат `/web/chat` (и строка ввода внизу панели — `?embed=1`) |
| `core/app/static/index.html` | Панель целиком (HTML+CSS+JS в одном файле): `GROUPS`, `load(t)`, `loadXxx()` на каждый подраздел |
| `core/app/media.py`, `sizes.py` | 1.10.3: сжатие фото и лимит файла; размеры базы и облака, предупреждения 70/90% (облако считает агент, `host.json`) |
| `core/app/balance.py` | 1.10.2: баланс GPTunnel (`/v1/balance`), история и прогноз, уведомления владельцу; кэш `app_state` `balance:*` |
| `core/app/market.py`, `envdata.py`, `net.py` | 1.10: курсы, крипта, индексы, ставка и инфляция (кэш `app_state` `market:*`), магнитные бури Kp, пыльца, поиск города; все внешние запросы — через `net.get` (тесты подменяют) |
| `core/app/release.py`, `scripts/host-agent.sh` | Версия, «что нового», обновление по кнопке |
| `core/migrations/NNN_*.sql` | Миграции, применяются при старте ядра. Только добавлять (откат кода должен работать с новой базой). `ro.*`-представления пересоздавать с `ro.can_see(...)` |
| `core/CHANGELOG.md`, `VERSION` | «Что нового» и номер версии (совпадает с `release.VERSION`, проверяет тест) |
| `tests/` | Проверки на настоящей PostgreSQL; `stubs/` — заглушки asyncpg (через psql), fastapi, httpx, pydantic_settings |
| `adapters/telegram/bot.py` | Адаптер Telegram, меню команд (`BotCommand`) |
| `core/app/static/site/` | Публичный сайт: `LOG` (история), `LANES` (дорожная карта), `FEATURES`, скриншоты `img/` |

Android-приложение — отдельный репозиторий `aasaidov38/assistant-android` (один `MainActivity.java`, мост `window.Android`).
Колбэки (`onVoice…`, `onShared`) он вызывает у главной страницы WebView.

## Правила, о которые легко споткнуться

- Кнопки: `action` ≤ 64 байт, формат `<префикс>:<id>:<действие>`. Занятые префиксы — в `brain.handle_action` (`c`, `y`, `e`, `u`…).
- Тексты бота — HTML Telegram: всё пользовательское через `util.esc`.
- Всё, что видит семья, — по-русски, просто, без жаргона. Личные данные: номера карт/документов, пароли, ПИН не хранить.
- Новый раздел в панели: секция `<section id>` + кнопка в `#tabs` с `data-g=<группа>` + `load()` + `prefs.TABS`.
- Не ломать адреса `/web/`, `/web/login`, `/web/chat`, `/web/api/chat/*`, заголовок `X-Requested-With: panel` (приложение).
- Изменения Caddy или `.env` кнопка обновления не делает — для таких версий отдельная инструкция для Claude Code.

## Как проверять, экономно

1. `bash tests/run_all.sh` — всегда. Новую функциональность покрывать в `tests/test_vXY.py` (по образцу `test_v18.py`: заглушка ИИ `AI[...]`, `check(...)`).
2. Панель — Playwright (`require('playwright')` из `$(npm root -g)`), страницы и API подменять через `page.route`.
   Проверять поведение текстом (`isVisible`, `textContent`, классы), скриншот — только 1–2 итоговых экрана.
3. Длинный вывод команд резать: `| tail`, `| grep -E "✖|ИТОГ"`.

## Выпуск

`docs/RELEASING.md`: VERSION + `release.VERSION` + раздел в `core/CHANGELOG.md` → тесты → `git push origin main` →
`git push origin main:stable` → проверить GitHub Actions (`gh run list -R aasaidov38/assistant --limit 2`).
Пуш тегов из песочницы не работает (прокси) — версии только через ветку `stable`.
В конце версии один раз обновить `assistant/project-state.md` в проекте и README (новый раздел).
