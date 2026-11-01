"""Push-уведомления в браузер и установленную панель (версия 1.8).

Стандарт Web Push: шифрование RFC 8291 (aes128gcm) и подпись VAPID (RFC 8292), без внешних сервисов —
браузер сам выдаёт адрес своего push-сервера (Google, Mozilla, Apple), мы шлём туда зашифрованное сообщение.
Ключи VAPID создаются один раз и лежат в базе (app_state 'vapid').

Нужна библиотека cryptography; если её нет — push просто выключен, остальное работает.
"""
import base64
import json
import logging
import os
import re
import struct
import time
from urllib.parse import urlparse

import httpx

from . import db
from .config import get_settings

log = logging.getLogger("push")

try:
    from cryptography.hazmat.primitives import hashes, hmac, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    AVAILABLE = True
except ImportError:          # pragma: no cover
    AVAILABLE = False

_client = httpx.AsyncClient(timeout=15)
MAX_FAILS = 5


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def unb64u(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _hmac(key: bytes, data: bytes) -> bytes:
    h = hmac.HMAC(key, hashes.SHA256())
    h.update(data)
    return h.finalize()


def _point(pub) -> bytes:
    return pub.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


# ---------- ключи VAPID ----------

async def vapid() -> dict | None:
    """{"d": закрытый ключ, "pub": открытый} в base64url; создаёт при первом вызове."""
    if not AVAILABLE:
        return None
    from . import release
    st = await release.get_state("vapid")
    if st.get("d") and st.get("pub"):
        return st
    key = ec.generate_private_key(ec.SECP256R1())
    st = {"d": b64u(key.private_numbers().private_value.to_bytes(32, "big")), "pub": b64u(_point(key.public_key()))}
    await release.set_state("vapid", st)
    log.info("Созданы ключи VAPID для push-уведомлений")
    return st


def _private(d: str):
    return ec.derive_private_key(int.from_bytes(unb64u(d), "big"), ec.SECP256R1())


def vapid_header(endpoint: str, keys: dict, now: int | None = None) -> str:
    u = urlparse(endpoint)
    s = get_settings()
    sub = s.public_url.rstrip("/") if s.public_url.startswith("https://") else "mailto:assistant@localhost"
    claims = {"aud": f"{u.scheme}://{u.netloc}", "exp": int(now or time.time()) + 12 * 3600, "sub": sub}
    head = b64u(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
    body = b64u(json.dumps(claims, separators=(",", ":")).encode())
    der = _private(keys["d"]).sign(f"{head}.{body}".encode(), ec.ECDSA(hashes.SHA256()))
    r, s_ = decode_dss_signature(der)
    sig = b64u(r.to_bytes(32, "big") + s_.to_bytes(32, "big"))
    return f"vapid t={head}.{body}.{sig}, k={keys['pub']}"


# ---------- шифрование RFC 8291 ----------

def encrypt(payload: bytes, p256dh: str, auth: str, salt: bytes | None = None, eph=None) -> bytes:
    ua_pub_raw, auth_secret = unb64u(p256dh), unb64u(auth)
    ua_pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_pub_raw)
    eph = eph or ec.generate_private_key(ec.SECP256R1())
    as_pub = _point(eph.public_key())
    secret = eph.exchange(ec.ECDH(), ua_pub)
    ikm = _hmac(_hmac(auth_secret, secret), b"WebPush: info\x00" + ua_pub_raw + as_pub + b"\x01")
    salt = salt or os.urandom(16)
    prk = _hmac(salt, ikm)
    cek = _hmac(prk, b"Content-Encoding: aes128gcm\x00\x01")[:16]
    nonce = _hmac(prk, b"Content-Encoding: nonce\x00\x01")[:12]
    ct = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)
    return salt + struct.pack("!I", 4096) + bytes([len(as_pub)]) + as_pub + ct


# ---------- подписки ----------

async def subscribe(user_id: int, sub: dict, user_agent: str = "") -> None:
    keys = sub.get("keys") or {}
    endpoint, p256dh, auth = sub.get("endpoint") or "", keys.get("p256dh") or "", keys.get("auth") or ""
    if not endpoint.startswith("https://") or len(endpoint) > 1000 or not p256dh or not auth:
        raise ValueError("Неверная подписка")
    if len(unb64u(p256dh)) != 65 or len(unb64u(auth)) != 16:
        raise ValueError("Неверные ключи подписки")
    await db.get_pool().execute(
        "INSERT INTO push_subscriptions (user_id, endpoint, p256dh, auth, user_agent) VALUES ($1,$2,$3,$4,$5)"
        " ON CONFLICT (endpoint) DO UPDATE SET user_id=$1, p256dh=$3, auth=$4, user_agent=$5, fails=0",
        user_id, endpoint, p256dh, auth, (user_agent or "")[:200])


async def unsubscribe(user_id: int, endpoint: str) -> None:
    await db.get_pool().execute("DELETE FROM push_subscriptions WHERE user_id=$1 AND endpoint=$2", user_id, endpoint)


async def count(user_id: int) -> int:
    return await db.get_pool().fetchval("SELECT count(*) FROM push_subscriptions WHERE user_id=$1", user_id) or 0


def _plain(html: str) -> str:
    t = re.sub(r"<br\s*/?>", "\n", html or "")
    t = re.sub(r"<[^>]+>", "", t)
    for a, b in (("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'"), ("&amp;", "&")):
        t = t.replace(a, b)
    return t.strip()


def message(text: str, url: str = "/web/chat") -> dict:
    """Заголовок — первая строка сообщения, текст — остальное."""
    lines = [ln for ln in _plain(text).splitlines() if ln.strip()]
    title = (lines[0] if lines else "Ассистент")[:80]
    body = "\n".join(lines[1:])[:300] if len(lines) > 1 else ""
    return {"title": title, "body": body, "url": url}


async def send(user_id: int, msg: dict, ttl: int = 86400) -> int:
    """Отправляет во все браузеры человека. Возвращает число доставленных."""
    if not AVAILABLE:
        return 0
    pool = db.get_pool()
    subs = await pool.fetch("SELECT id, endpoint, p256dh, auth FROM push_subscriptions WHERE user_id=$1", user_id)
    if not subs:
        return 0
    keys = await vapid()
    data = json.dumps(msg, ensure_ascii=False).encode()
    ok = 0
    for s in subs:
        try:
            r = await _client.post(s["endpoint"], content=encrypt(data, s["p256dh"], s["auth"]), headers={
                "Authorization": vapid_header(s["endpoint"], keys), "Content-Encoding": "aes128gcm",
                "Content-Type": "application/octet-stream", "TTL": str(ttl), "Urgency": "normal"})
        except Exception as e:
            log.warning("Push не отправлен: %s", e)
            await pool.execute("UPDATE push_subscriptions SET fails=fails+1 WHERE id=$1", s["id"])
            continue
        if r.status_code in (200, 201, 202):
            ok += 1
            await pool.execute("UPDATE push_subscriptions SET last_ok=now(), fails=0 WHERE id=$1", s["id"])
        elif r.status_code in (404, 410):          # браузер отписался или подписка устарела
            await pool.execute("DELETE FROM push_subscriptions WHERE id=$1", s["id"])
        else:
            log.warning("Push %s: %s", r.status_code, r.text[:200])
            await pool.execute("UPDATE push_subscriptions SET fails=fails+1 WHERE id=$1", s["id"])
    await pool.execute("DELETE FROM push_subscriptions WHERE user_id=$1 AND fails >= $2", user_id, MAX_FAILS)
    return ok
