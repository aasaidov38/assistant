"""API для телефонов (версия 1.9).

Панель (вход через бота):   GET/POST /web/api/devices, POST /web/api/devices/{id}/revoke, GET /web/api/bankfeed
Телефон (заголовок X-Device-Key): GET /web/api/device/config, POST /web/api/device/{health|bank|sim|status}
Адреса под /web/ — их уже пропускает Caddy, менять его настройки не нужно.
"""
from fastapi import APIRouter, Depends, Header, HTTPException

from . import bankfeed, devices, health, simcards, users
from .web import current_user
from .web_life import jsonable

router = APIRouter()


async def device_auth(x_device_key: str = Header(default="")) -> dict:
    d = await devices.by_key(x_device_key.strip())
    if not d:
        raise HTTPException(401, "Ключ телефона не подходит или отозван — подключите телефон заново в «Интеграциях»")
    return d


# ---------- панель ----------

@router.get("/web/api/devices")
async def devices_list(user: users.User = Depends(current_user)):
    return jsonable({"devices": await devices.list_family(user)})


@router.post("/web/api/devices")
async def devices_create(body: dict, user: users.User = Depends(current_user)):
    info = body.get("info") if isinstance(body.get("info"), dict) else {}
    did, key = await devices.create(user, str(body.get("name") or ""), {})
    await devices.save_info(did, info)
    return {"id": did, "key": key}


@router.post("/web/api/devices/{device_id}/revoke")
async def devices_revoke(device_id: int, user: users.User = Depends(current_user)):
    if not await devices.revoke(user, device_id):
        raise HTTPException(404, "Нет такого телефона или отозвать может только его хозяин и владелец")
    return {"ok": True, "text": "Доступ телефона отозван."}


@router.get("/web/api/bankfeed")
async def bankfeed_overview(user: users.User = Depends(current_user)):
    return jsonable(await bankfeed.overview(user))


# ---------- телефон ----------

@router.get("/web/api/device/config")
async def device_config(device: dict = Depends(device_auth)):
    return await devices.config(device)


@router.post("/web/api/device/status")
async def device_status(body: dict, device: dict = Depends(device_auth)):
    await devices.save_info(device["id"], body)
    return await devices.config(device)


@router.post("/web/api/device/health")
async def device_health(body: dict, device: dict = Depends(device_auth)):
    return await health.ingest(device, body.get("items") or [])


@router.post("/web/api/device/bank")
async def device_bank(body: dict, device: dict = Depends(device_auth)):
    return jsonable(await bankfeed.ingest(device, body.get("items") or []))


@router.post("/web/api/device/sim")
async def device_sim(body: dict, device: dict = Depends(device_auth)):
    return {"sims": await simcards.ingest(device, body.get("items") or [])}
