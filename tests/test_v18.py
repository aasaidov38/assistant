"""Версия 1.8: уведомления о новой версии, обновление по кнопке, статус копий — на настоящей PostgreSQL."""
import _env  # noqa: F401  (пути и база)
import asyncio, json, os, subprocess, tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
import asyncpg
from app import ai, brain, cards, db, notify, push, release, scope, undo, users, web_life
from app.schemas import Action

from _env import PSQL
sql = lambda s: subprocess.run(PSQL, input=s, capture_output=True, text=True, check=True).stdout.strip()
FAILS, SENT = [], []


def check(cond, name, info=""):
    print(("  ✔ " if cond else "  ✖ ") + name + ("" if cond else f"  → {str(info)[:500]}"))
    if not cond:
        FAILS.append(name)


async def fake_deliver(uid, reply):
    SENT.append((uid, reply.text, [b.action for r in reply.buttons for b in r if b.action]))
    return True


SEED = """
TRUNCATE families, users, user_identities, outbox, app_state, loyalty_cards, push_subscriptions RESTART IDENTITY CASCADE;
INSERT INTO families (name) VALUES ('Демо');
INSERT INTO users (family_id, name) VALUES (1,'Алексей'), (1,'Мария');
INSERT INTO user_identities (channel, external_id, user_id) VALUES ('telegram','111',1),('telegram','222',2);
"""

CHANGELOG = """# История
## 1.9 — 10.12.2026
- Новая штука
- Вторая <важная>

## 1.8 — 01.11.2026
- Карты лояльности
- Голос удержанием

## 1.7.2 — 01.11.2026
- Сайт
"""

iso = lambda dt: dt.isoformat()
utc = lambda: datetime.now(timezone.utc)


def put(name, data):
    (release.STATE / name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


async def press(uid, action):
    return await brain.handle_action(Action(external_user_id=str(uid), action=action))


AI = {}


async def fake_json(system, text, **k):
    for key, val in AI.items():
        if key in text:
            return val
    raise AssertionError("нет заготовки ИИ для: " + text[:80])


async def main():
    ai.complete_json = fake_json
    db.pool = asyncpg.FakeConn()
    sql(SEED)
    notify.deliver = fake_deliver
    tmp = Path(tempfile.mkdtemp())
    release.STATE = tmp
    alex, maria = await users.get(1), await users.get(2)

    print("1. Список изменений")
    check(release.notes("1.9", CHANGELOG) == ["Новая штука", "Вторая <важная>"], "заметки 1.9", release.notes("1.9", CHANGELOG))
    check(release.notes("1.7.2", CHANGELOG) == ["Сайт"], "заметки 1.7.2 (три цифры)")
    check(release.notes("2.0", CHANGELOG) == [], "нет версии — пусто")
    check(release.notes(release.VERSION), "в образе есть CHANGELOG с текущей версией", release.CHANGELOG)
    check(release.newer("1.10", "1.9") and not release.newer("1.8", "1.8") and release.newer("1.7.2", "1.7"),
          "сравнение версий 1.10 > 1.9, 1.7.2 > 1.7")
    root = Path(_env.HERE).parent
    check((root / "VERSION").read_text().strip() == release.VERSION, "файл VERSION совпадает с release.VERSION")

    print("2. Объявление после обновления")
    SENT.clear(); await release.announce_if_updated()
    check(not SENT, "первая установка — без объявления", SENT)
    sql("UPDATE app_state SET value='{\"version\":\"1.7.2\"}' WHERE key='announced'")
    SENT.clear(); await release.announce_if_updated()
    check(len(SENT) == 2 and "обновлён до версии " + release.VERSION in SENT[0][1], "после обновления — всем в семье", SENT)
    SENT.clear(); await release.announce_if_updated()
    check(not SENT, "второй запуск той же версии — тишина", SENT)
    sql("UPDATE app_state SET value='{\"version\":\"9.0\"}' WHERE key='announced'")
    SENT.clear(); await release.announce_if_updated()
    check(not SENT, "откат на старую версию — тишина", SENT)

    print("3. Предложение обновиться")
    SENT.clear(); await release.check()
    check(not SENT, "нет файла от агента — ничего", SENT)
    put("update-available.json", {"version": release.VERSION, "notes": []})
    SENT.clear(); await release.check()
    check(not SENT, "та же версия — ничего", SENT)
    put("update-available.json", {"version": "9.9", "notes": ["Новая штука", "<b>x</b>"]})
    SENT.clear(); await release.check()
    check(len(SENT) == 1 and SENT[0][0] == 1 and "9.9" in SENT[0][1] and "&lt;b&gt;" in SENT[0][1]
          and "y:9.9:go" in SENT[0][2] and "y:9.9:later" in SENT[0][2], "владельцу: что нового и две кнопки", SENT)
    SENT.clear(); await release.check()
    check(not SENT, "повторно сразу — тишина", SENT)
    st = json.loads(sql("select value from app_state where key='update_offer'"))
    st["next_at"] = iso(utc() - timedelta(minutes=1))
    sql(f"UPDATE app_state SET value='{json.dumps(st)}' WHERE key='update_offer'")
    SENT.clear(); await release.check()
    check(len(SENT) == 1 and "Напоминаю" in SENT[0][1], "через 3 дня — напоминание", SENT)

    print("4. Кнопки")
    r = await press(222, "y:9.9:go")
    check("только владелец" in r.text, "Мария не может обновить", r.text)
    r = await press(111, "y:9.9:go")
    check("Агент обновлений" in r.text and not (tmp / "update-request.json").exists(), "агент молчит — честно говорим", r.text)
    put("host.json", {"at": iso(utc()), "version": release.VERSION,
                      "local_backup": {"at": iso(utc() - timedelta(hours=3)), "size_mb": 4.2},
                      "offsite": {"ok_at": iso(utc() - timedelta(hours=40))}, "disk": {"free_gb": 21.5}})
    r = await press(111, "y:9.9:later")
    check("напомню через 3" in r.text, "«Напомнить позже»", r.text)
    r = await press(111, "y:9.9:go")
    req = json.loads((tmp / "update-request.json").read_text()) if (tmp / "update-request.json").exists() else {}
    check("Обновляю до 9.9" in r.text and req.get("version") == "9.9", "владелец — команда агенту записана", (r.text, req))
    r = await press(111, "y:9.9:go")
    check("уже запущено" in r.text, "двойное нажатие", r.text)
    r = await press(111, "y:5.0:go")
    check("не актуальна" in r.text, "кнопка со старой версией", r.text)
    SENT.clear(); await release.check()
    check(not SENT, "после запроса напоминаний нет", SENT)

    print("5. Итог обновления")
    (tmp / "update-request.json").unlink()
    put("update-result.json", {"version": "9.9", "from": release.VERSION, "ok": False,
                               "message": "Новая версия не запустилась", "at": iso(utc())})
    SENT.clear(); await release.check()
    check(len(SENT) == 1 and "не получилось" in SENT[0][1] and "не запустилась" in SENT[0][1], "неудача — владельцу", SENT)
    SENT.clear(); await release.check()
    check(not SENT, "о той же неудаче — один раз", SENT)
    st = json.loads(sql("select value from app_state where key='update_offer'"))
    check(not st.get("requested") and st.get("next_at"), "после неудачи снова напомним позже", st)
    put("update-result.json", {"version": "9.9", "from": release.VERSION, "ok": True, "message": "ok", "at": iso(utc() + timedelta(seconds=1))})
    SENT.clear(); await release.check()
    check(not SENT, "удача — объявит сама новая версия при старте", SENT)

    print("6. /version")
    r = await brain.handle_text(alex, "/version", source="text")
    check(f"Версия ассистента: {release.VERSION}" in r.text and "Доступна 9.9" in r.text and "4.2 МБ" in r.text
          and "давно" in r.text and "21.5 ГБ" in r.text, "/version: версия, обновление, копии, диск", r.text)
    check(any(b.action == "y:9.9:go" for row in r.buttons for b in row), "у владельца — кнопка обновления")
    r = await brain.handle_text(maria, "/version", source="text")
    check(not r.buttons, "у Марии кнопки нет", r.buttons)
    put("host.json", {"at": iso(utc() - timedelta(hours=1))})
    r = await brain.handle_text(alex, "/version", source="text")
    check("Агент на сервере не отвечает" in r.text and "не настроено" in r.text, "агент давно молчал", r.text)

    print("7. Панель: настройки")
    put("host.json", {"at": iso(utc()), "local_backup": {"at": iso(utc())}, "disk": {"free_gb": 20}})
    await scope.enter(alex); s = (await web_life.settings_get(user=alex))["system"]
    check(s["version"] == release.VERSION and s["available"]["version"] == "9.9" and s["can_update"], "владелец видит обновление", s)
    r = await web_life.update_start({"version": "9.9"}, user=alex)
    check("Обновляю" in r["text"] and (tmp / "update-request.json").exists(), "кнопка в панели запускает обновление", r)
    await scope.enter(maria); s = (await web_life.settings_get(user=maria))["system"]
    check(not s["can_update"] and s["updating"], "Мария: без кнопки, видит «идёт обновление»", s)
    scope.leave()

    print("8. Голос из браузера")
    check(ai.audio_name("audio/webm;codecs=opus") == ("voice.webm", "audio/webm"), "webm из Chrome", ai.audio_name("audio/webm;codecs=opus"))
    check(ai.audio_name("audio/mp4")[0] == "voice.m4a" and ai.audio_name(None)[0] == "voice.ogg" and ai.audio_name("x/y")[0] == "voice.ogg",
          "Safari mp4 → m4a, по умолчанию ogg")
    html = (root / "core/app/static/chat.html").read_text()
    check("MediaRecorder" in html and "pointerdown" in html and "setPointerCapture" in html, "чат: удержание и запись в браузере")

    print("9. Карты лояльности: номера и форматы")
    check(cards.clean("2775 0123 4567 5") == ("2775012345675", "ean13"), "13 цифр с верной контрольной → EAN-13")
    check(cards.clean("2775 0123 4567 8") == ("2775012345678", "code128"), "контрольная не сошлась → обычный штрихкод")
    check(cards.clean("46070123")[1] == "code128" and cards.clean("96385074")[1] == "ean8", "EAN-8 только при верной контрольной")
    check(cards.clean("AB-12345")[1] == "code128" and cards.clean("Клуб 15%")[1] == "qr" and cards.clean("x", "text")[1] == "text",
          "буквы → Code128, кириллица → QR, «только номер»")
    try:
        cards.clean("4276 3800 1234 5679"); bank = False
    except cards.CardError as e:
        bank = "банковской" in str(e)
    check(bank, "номер банковской карты (Луна, 16 цифр) не принимается")
    check(not cards.looks_like_bank_card("7799123456789012") and not cards.looks_like_bank_card("2775012345675"),
          "карты магазинов (77…, 13 цифр) за банковские не принимаются")

    print("10. Карты: бот")
    sql("UPDATE app_state SET value='{}' WHERE key='update_offer'")
    AI["карта Ленты"] = {"intent": "card_add", "card_add": {"store": "Лента", "number": "2775 0123 4567 5"}}
    r = await brain.handle_text(alex, "карта Ленты 2775 0123 4567 5", source="text")
    check("Сохранил карту <b>Лента</b>" in r.text and "EAN-13" in r.text and any(b.action == "u:c.1:del" for row in r.buttons for b in row),
          "«карта Ленты …» сохраняет, есть отмена", r.text)
    r = await brain.handle_text(alex, "карта Ленты 2775 0123 4567 5", source="text")
    check("уже есть" in r.text, "повтор — не дублирует", r.text)
    AI["карта Пятёрочки"] = {"intent": "card_add", "card_add": {"store": "Пятёрочка", "number": "7799 1234 5678"}}
    await brain.handle_text(maria, "карта Пятёрочки 7799 1234 5678", source="text")
    AI["банк"] = {"intent": "card_add", "card_add": {"store": "Сбер", "number": "4276380012345679"}}
    r = await brain.handle_text(alex, "банк 4276380012345679", source="text")
    check("не храню" in r.text and sql("select count(*) from loyalty_cards") == "2", "банковскую карту бот не сохраняет", r.text)
    AI["покажи карту ленты"] = {"intent": "card_show", "card_show": {"store": "ленты"}}
    r = await brain.handle_text(maria, "покажи карту ленты", source="text")
    check("Лента" in r.text and "2775012345675" in r.text, "Мария видит карту Алексея (раздел общий)", r.text)
    check(sql("select used_count from loyalty_cards where id=1") == "1", "показ считает использование")
    AI["покажи карту пятерочки"] = {"intent": "card_show", "card_show": {"store": "пятерочки"}}
    r = await brain.handle_text(alex, "покажи карту пятерочки", source="text")
    check("Пятёрочка" in r.text, "«пятерочки» найдёт «Пятёрочка»", r.text)
    AI["карту магнита"] = {"intent": "card_show", "card_show": {"store": "магнита"}}
    r = await brain.handle_text(alex, "карту магнита", source="text")
    check("не нашёл" in r.text and "c:1:show" in [b.action for row in r.buttons for b in row], "нет карты — список кнопками", r.text)
    r = await brain.handle_text(alex, "/cards", source="text")
    check("Карты лояльности</b> (2)" in r.text and r.text.index("Лента") < r.text.index("Пятёрочка"), "/cards: частые сверху", r.text)
    r = await press(111, "c:2:show")
    check("Пятёрочка" in r.text and "779912345678" in r.text, "кнопка карты", r.text)

    print("11. Карты: фото, личный режим, отмена, панель")
    AI["Разбери фото"] = {"type": "card", "store": "Магнит", "number": "4607012345677", "format": "ean13"}
    r = await brain.handle_photo(alex, "eA==", "image/jpeg", "")
    check("Магнит" in r.text and "EAN-13" in r.text, "фото карты → сохранена", r.text)
    op, _ = await undo.cancel(alex, "c.3")
    check(op and op["cancelled"] and len(await cards.cards(1)) == 2, "отмена удаляет карту", op)
    await undo.restore(alex, "c.3")
    h = await undo.history(1, types=["card"])
    check(len(h) == 3 and h[0]["title"].startswith("💳"), "карты в истории", h[:2])
    sql("UPDATE families SET sections = sections || '{\"cards\":\"personal\"}'::jsonb")
    await scope.enter(maria)
    check([c["store"] for c in await cards.cards(1)] == ["Пятёрочка"], "личный режим: Мария видит только свою")
    sql("UPDATE families SET sections = sections - 'cards'")
    await scope.enter(alex)
    d = await web_life.cards_list(user=alex)
    check(len(d["cards"]) == 3 and d["cards"][0]["store"] == "Пятёрочка" and d["cards"][0]["ref"] == "c.2"
          and not d["cards"][0]["mine"] and not d["personal"], "API: список карт", d)
    r = await web_life.cards_add(web_life.CardIn(store="Летуаль", number="ABC-77"), user=alex)
    check(r["ok"] and "Code" not in r["text"] and "штрихкод" in r["text"], "API: добавить карту", r)
    r = await web_life.cards_add(web_life.CardIn(store="Банк", number="5469 3800 1234 5673"), user=alex)
    check(not r["ok"] and "не храню" in r["text"], "API: банковская карта отклонена", r)
    await web_life.cards_used(1, user=alex)
    check(sql("select used_count from loyalty_cards where id=1") == "2", "API: отметка использования")
    scope.leave()

    print("12. Push: шифрование и подпись")
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    ua = ec.generate_private_key(ec.SECP256R1()); ua_pub = push._point(ua.public_key()); auth = os.urandom(16)
    body = push.encrypt("Привет 👋".encode(), push.b64u(ua_pub), push.b64u(auth))
    salt, rs, idlen = body[:16], int.from_bytes(body[16:20], "big"), body[20]
    as_pub = body[21:21 + idlen]; ct = body[21 + idlen:]
    sec = ua.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_pub))
    ikm = push._hmac(push._hmac(auth, sec), b"WebPush: info\x00" + ua_pub + as_pub + b"\x01")
    prk = push._hmac(salt, ikm)
    pt = AESGCM(push._hmac(prk, b"Content-Encoding: aes128gcm\x00\x01")[:16]).decrypt(
        push._hmac(prk, b"Content-Encoding: nonce\x00\x01")[:12], ct, None)
    check(rs == 4096 and idlen == 65 and pt == "Привет 👋".encode() + b"\x02", "браузер расшифровывает (RFC 8291)", pt)
    eph = ec.derive_private_key(int.from_bytes(push.unb64u("yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"), "big"), ec.SECP256R1())
    out = push.encrypt(b"When I grow up, I want to be a watermelon",
                       "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4",
                       "BTBZMqHH6r4Tts7J_aSIgg", salt=push.unb64u("DGv6ra1nlYgDCS1FRnbzlw"), eph=eph)
    check(push.b64u(out) == "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A_"
          "yl95bQpu6cVPTpK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN", "пример из RFC 8291 (приложение A) совпал")
    keys = await push.vapid()
    check(keys == await push.vapid() and len(push.unb64u(keys["pub"])) == 65, "ключи VAPID создаются один раз")
    hdr = push.vapid_header("https://fcm.googleapis.com/fcm/send/abc", keys)
    jwt = hdr.split("t=")[1].split(",")[0]; h_, b_, sig = jwt.split(".")
    claims = json.loads(push.unb64u(b_)); raw = push.unb64u(sig)
    pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), push.unb64u(keys["pub"]))
    try:
        pub.verify(encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")),
                   f"{h_}.{b_}".encode(), ec.ECDSA(hashes.SHA256())); good = True
    except Exception:
        good = False
    check(good and claims["aud"] == "https://fcm.googleapis.com" and hdr.endswith("k=" + keys["pub"]), "подпись VAPID верная", claims)
    m = push.message("⏰ <b>Через 3 дн.</b> вернуть Ивану\n10 000 ₽ &amp; ещё")
    check(m["title"] == "⏰ Через 3 дн. вернуть Ивану" and m["body"] == "10 000 ₽ & ещё", "текст без HTML", m)

    print("13. Push: подписки и отправка")
    POSTS = []

    class FakeResp:
        def __init__(self, code): self.status_code, self.text = code, ""

    class FakeClient:
        async def post(self, url, content=None, headers=None):
            POSTS.append((url, headers)); return FakeResp(410 if "dead" in url else 201)
    push._client = FakeClient()
    sub = {"endpoint": "https://push.example/ok", "keys": {"p256dh": push.b64u(ua_pub), "auth": push.b64u(auth)}}
    await push.subscribe(1, sub)
    await push.subscribe(1, {**sub, "endpoint": "https://push.example/dead"})
    try:
        await push.subscribe(1, {"endpoint": "http://x", "keys": {}}); bad = False
    except ValueError:
        bad = True
    check(bad and await push.count(1) == 2, "две подписки, кривая отклонена")
    SENT.clear(); POSTS.clear()
    sql("UPDATE users SET settings = settings - 'quiet'")
    await notify.send(1, release.Reply(text="⏰ Напоминание\nоплатить интернет"), topic="bills", urgent=True)
    check(len(SENT) == 1 and len(POSTS) == 2 and POSTS[0][1]["Content-Encoding"] == "aes128gcm" and "vapid t=" in POSTS[0][1]["Authorization"],
          "напоминание: Telegram + push", POSTS)
    check(await push.count(1) == 1, "подписка с ответом 410 удалена")
    POSTS.clear(); await notify.send(1, release.Reply(text="копия"), quiet="drop")
    check(not POSTS, "копии из панели в push не идут")
    await scope.enter(alex)
    I = await web_life.integrations(user=alex)
    check(I["push"]["available"] and I["push"]["key"] == keys["pub"] and I["push"]["count"] == 1 and I["telegram"]["connected"]
          and len(I["soon"]) >= 2, "API интеграций", I)
    r = await web_life.push_test(user=alex)
    check(r["ok"], "пробное уведомление", r)
    await web_life.push_unsubscribe({"endpoint": "https://push.example/ok"}, user=alex)
    check(await push.count(1) == 0, "отписка")
    scope.leave()

    print("ИТОГ:", "всё прошло" if not FAILS else FAILS)


asyncio.run(main())
