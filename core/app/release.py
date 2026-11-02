"""Версии и обновления (1.8).

VERSION — версия этого кода. Список изменений — файл core/CHANGELOG.md (он же источник «Что нового»).

Как устроено обновление по кнопке:
  1. На сервере (не в контейнере) раз в минуту работает scripts/host-agent.sh. Раз в 30 минут он смотрит
     в GitHub, есть ли тег новее текущего, и пишет state/update-available.json (версия и «что нового»).
     Ещё он пишет state/host.json: когда была последняя копия сервера, копия в Яндексе, место на диске.
  2. Ядро видит этот файл и присылает владельцу: «Вышла версия …, обновить?» с кнопкой. Если не обновили —
     напоминает раз в 3 дня.
  3. Кнопка «Обновить» кладёт state/update-request.json. Агент делает копию базы, переключает код на новый
     тег и пересобирает контейнеры. Если ядро не поднялось с новой версией — возвращает прежнюю и пишет
     state/update-result.json с причиной.
  4. После перезапуска ядро само сообщает всей семье «Ассистент обновлён до …» со списком изменений.
Папка state — общая для сервера и ядра (в контейнере /srv/state).
"""
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import db, notify, users
from .config import get_settings
from .schemas import Button, Reply
from .util import esc

log = logging.getLogger("release")

VERSION = "1.9.1"
HERE = Path(__file__).resolve().parent
CHANGELOG = HERE.parent / "CHANGELOG.md"
STATE = HERE.parent / "state"
REMIND_DAYS = 3


def vtuple(v: str | None) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v or "")[:4]) or (0,)


def newer(a: str | None, b: str | None) -> bool:
    """a новее b?"""
    return vtuple(a) > vtuple(b)


def notes(version: str, text: str | None = None, limit: int = 8) -> list[str]:
    """Пункты «что нового» для версии из CHANGELOG.md (раздел «## 1.8 …»)."""
    try:
        text = text if text is not None else CHANGELOG.read_text(encoding="utf-8")
    except OSError:
        return []
    out, inside = [], False
    for line in text.splitlines():
        if line.startswith("## "):
            if inside:
                break
            inside = vtuple(line[3:].split()[0] if line[3:].split() else "") == vtuple(version)
            continue
        if inside and line.lstrip().startswith(("- ", "* ")):
            out.append(line.lstrip()[2:].strip())
    return out[:limit]


# ---------- служебные значения в базе ----------

async def get_state(key: str) -> dict:
    v = await db.get_pool().fetchval("SELECT value FROM app_state WHERE key=$1", key)
    if isinstance(v, str):
        v = json.loads(v or "{}")
    return v or {}


async def set_state(key: str, value: dict) -> None:
    await db.get_pool().execute(
        "INSERT INTO app_state (key, value, updated_at) VALUES ($1, $2::jsonb, now())"
        " ON CONFLICT (key) DO UPDATE SET value=$2::jsonb, updated_at=now()", key, value)


# ---------- файлы агента ----------

def read_file(name: str) -> dict | None:
    try:
        return json.loads((STATE / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_file(name: str, data: dict) -> bool:
    try:
        STATE.mkdir(exist_ok=True)
        tmp = STATE / (name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(STATE / name)
        return True
    except OSError as e:
        log.warning("Не записать %s: %s", name, e)
        return False


def host() -> dict:
    """Состояние сервера от агента: копии, диск, когда агент был последний раз."""
    h = read_file("host.json") or {}
    if h.get("at"):
        try:
            h["agent_ok"] = datetime.now(timezone.utc) - datetime.fromisoformat(h["at"]) < timedelta(minutes=15)
        except ValueError:
            h["agent_ok"] = False
    else:
        h["agent_ok"] = False
    return h


def available() -> dict | None:
    a = read_file("update-available.json")
    return a if a and newer(a.get("version"), VERSION) else None


# ---------- сообщения ----------

def _list(items: list[str]) -> str:
    return "\n".join("• " + esc(i) for i in items) if items else "• исправления и улучшения"


async def _owner(family_id: int | None = None):
    rows = await db.get_pool().fetch("SELECT DISTINCT family_id FROM users ORDER BY family_id")
    out = []
    for r in rows:
        if family_id and r["family_id"] != family_id:
            continue
        ms = await users.family_members(r["family_id"])
        if ms:
            out.append(ms[0])
    return out


def _site_button() -> list[list[Button]]:
    url = get_settings().public_url
    return [[Button(text="📄 Подробнее на сайте", url=url.rstrip("/") + "/#changelog")]] if url else []


async def announce_if_updated() -> None:
    """После обновления: всем членам семьи «Ассистент обновлён до …» (один раз на версию)."""
    st = await get_state("announced")
    prev = st.get("version")
    if prev == VERSION:
        return
    await set_state("announced", {"version": VERSION, "at": datetime.now(timezone.utc).isoformat()})
    if not prev or not newer(VERSION, prev):
        return            # первая установка или откат — молчим
    text = f"🎉 <b>Ассистент обновлён до версии {VERSION}</b>\n\nЧто нового:\n{_list(notes(VERSION))}"
    if get_settings().public_url:
        text += "\n\nЕсли панель открыта в браузере или приложении — обновите страницу."
    for row in await db.get_pool().fetch("SELECT id FROM users ORDER BY id"):
        try:
            await notify.send(row["id"], Reply(text=text, buttons=_site_button()), topic="updates")
        except Exception:
            log.exception("Не отправил «что нового» пользователю %s", row["id"])


def _offer(a: dict, again: bool = False) -> Reply:
    v = a["version"]
    head = f"🔔 Напоминаю: доступна новая версия <b>{esc(v)}</b>" if again else f"🆕 <b>Вышла новая версия ассистента {esc(v)}</b>"
    text = head + f" (сейчас {VERSION})\n\n" \
        + f"Что нового:\n{_list(a.get('notes') or [])}\n\n" \
        + "Обновить сейчас? Перед обновлением сделаю копию базы. Если новая версия не запустится, " \
          "сам верну прежнюю и напишу, что случилось. Обычно занимает 2–5 минут."
    return Reply(text=text, buttons=[[Button(text="🔄 Обновить сейчас", action=f"y:{v}:go")],
                                     [Button(text="⏰ Напомнить позже", action=f"y:{v}:later")]] + _site_button())


async def check() -> None:
    """Фоновая задача: новая версия, напоминания, итог обновления."""
    # итог обновления, которое не удалось (удачное объявит новая версия при старте)
    res = read_file("update-result.json")
    if res and res.get("at"):
        seen = await get_state("update_result")
        if seen.get("at") != res["at"]:
            await set_state("update_result", {"at": res["at"]})
            if not res.get("ok"):
                off = await get_state("update_offer")
                if off.get("requested"):
                    nxt = datetime.now(timezone.utc) + timedelta(days=REMIND_DAYS)
                    await set_state("update_offer", {**off, "requested": False, "next_at": nxt.isoformat()})
                for o in await _owner():
                    await notify.send(o.id, Reply(text=(
                        f"⚠️ <b>Обновить до {esc(res.get('version') or '?')} не получилось</b>\n"
                        f"Вернул прежнюю версию {esc(res.get('from') or VERSION)}, всё работает как раньше.\n\n"
                        f"Причина: {esc((res.get('message') or 'нет подробностей')[:600])}\n\n"
                        "Пришлите это сообщение в чат с Claude — разберёмся.")), urgent=True)
    a = available()
    if not a:
        return
    st = await get_state("update_offer")
    now = datetime.now(timezone.utc)
    if st.get("version") == a["version"]:
        nxt = st.get("next_at")
        if st.get("requested") or (nxt and now < datetime.fromisoformat(nxt)):
            return
        again = True
    else:
        again = False
    await set_state("update_offer", {"version": a["version"], "next_at": (now + timedelta(days=REMIND_DAYS)).isoformat()})
    for o in await _owner():
        await notify.send(o.id, _offer(a, again), topic="updates")


async def is_owner(user) -> bool:
    ms = await users.family_members(user.family_id)
    return bool(ms) and ms[0].id == user.id


async def request_update(user, version: str) -> Reply:
    if not await is_owner(user):
        return Reply(text="Обновлять ассистента может только владелец.")
    a = available()
    if not a or a["version"] != version:
        return Reply(text=f"Версия {esc(version)} уже установлена или больше не актуальна. Сейчас: {VERSION}.")
    h = host()
    if not h.get("agent_ok"):
        return Reply(text="⚠️ Агент обновлений на сервере сейчас не отвечает, обновить по кнопке не получится. "
                          "Обновите через Claude Code (docs/DEPLOY_CLAUDE_CODE.md).")
    if read_file("update-request.json"):
        return Reply(text="⏳ Обновление уже запущено, подождите пару минут.")
    if not write_file("update-request.json", {"version": version, "by": user.name,
                                              "at": datetime.now(timezone.utc).isoformat()}):
        return Reply(text="⚠️ Не получилось передать команду серверу (нет доступа к папке state).")
    st = await get_state("update_offer")
    await set_state("update_offer", {**st, "version": version, "requested": True})
    return Reply(text=f"⏳ Обновляю до {esc(version)}: копия базы, новый код, перезапуск. "
                      "Это 2–5 минут, бот на это время замолчит. Когда закончу — пришлю «что нового».")


async def postpone(user, version: str) -> Reply:
    st = await get_state("update_offer")
    nxt = datetime.now(timezone.utc) + timedelta(days=REMIND_DAYS)
    await set_state("update_offer", {**st, "version": version, "next_at": nxt.isoformat(), "requested": False})
    return Reply(text=f"Хорошо, напомню через {REMIND_DAYS} дня. Обновить можно и раньше: /version")


async def handle_action(user, parts: list[str]) -> Reply:
    """y:<версия>:go | y:<версия>:later"""
    if parts[2] == "go":
        return await request_update(user, parts[1])
    if parts[2] == "later":
        return await postpone(user, parts[1])
    return Reply(text="Кнопка устарела.")


def _ago(iso: str | None) -> str:
    if not iso:
        return "нет данных"
    try:
        t = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    h = (datetime.now(timezone.utc) - t).total_seconds() / 3600
    when = t.astimezone(get_settings().zone).strftime("%d.%m %H:%M")
    return f"{when}" + (" ⚠️ давно" if h > 30 else "")


async def status(owner: bool) -> dict:
    """Для панели: версия, обновление, копии, диск, агент."""
    h, a = host(), available()
    lb, off = h.get("local_backup") or {}, h.get("offsite") or {}
    return {"version": VERSION, "available": a, "can_update": bool(a and owner and h.get("agent_ok")),
            "updating": bool(read_file("update-request.json")), "agent_ok": h.get("agent_ok", False),
            "local_backup": lb, "offsite": off, "disk": h.get("disk") or {}}


async def version_reply(user) -> Reply:
    """/version — версия, доступное обновление, состояние копий."""
    h = host()
    a = available()
    lines = [f"🏷 <b>Версия ассистента: {VERSION}</b>"]
    if a:
        lines.append(f"🆕 Доступна {esc(a['version'])}:\n{_list(a.get('notes') or [])}")
    elif h.get("agent_ok"):
        lines.append("✅ Это последняя версия.")
    lines.append("")
    lines.append("🗄 <b>Резервные копии</b>")
    lb, off = h.get("local_backup") or {}, h.get("offsite") or {}
    lines.append(f"• На сервере: {_ago(lb.get('at'))}" + (f", {lb['size_mb']} МБ" if lb.get("size_mb") else ""))
    lines.append(f"• В Яндекс Облаке: {_ago(off.get('ok_at')) if off.get('ok_at') else 'не настроено'}")
    if h.get("disk"):
        lines.append(f"• Свободно на диске: {h['disk'].get('free_gb')} ГБ")
    if not h.get("agent_ok"):
        lines.append("\n⚠️ Агент на сервере не отвечает: копии и обновления не отслеживаются. "
                     "Установка — docs/DEPLOY_CLAUDE_CODE.md, шаг «Агент».")
    btn = [[Button(text=f"🔄 Обновить до {a['version']}", action=f"y:{a['version']}:go")]] \
        if a and await is_owner(user) else []
    return Reply(text="\n".join(lines), buttons=btn)
