import os, asyncio, logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("paginatto")

# ====== Config ======
ZAPI_INSTANCE = os.getenv("ZAPI_INSTANCE", "")
ZAPI_TOKEN = os.getenv("ZAPI_TOKEN", "")
ZAPI_CLIENT_TOKEN = os.getenv("ZAPI_CLIENT_TOKEN", "")
SENDER_NAME = os.getenv("WHATSAPP_SENDER_NAME", "Paginatto")
MSG_TEMPLATE = os.getenv("MSG_TEMPLATE",
    "Oi {name}! Você deixou {product} no carrinho da {brand} por {price}. "
    "Finaliza aqui: {checkout_url} (qualquer dúvida me chama!)"
)

ZAPI_URL = f"https://api.z-api.io/instances/{ZAPI_INSTANCE}/token/{ZAPI_TOKEN}/send-text"

# ====== App & Agenda ======
app = FastAPI(title="Paginatto - Carrinho Abandonado", version="0.1.0")
scheduler = AsyncIOScheduler()
scheduler.start()

# Guardamos jobs por order_id para poder cancelar se pagar
PENDING_JOBS: Dict[str, str] = {}  # order_id -> job_id

@app.get("/health")
def health():
    return {"ok": True, "service": "paginatto", "version": "0.1.0"}

# ---------- Helpers ----------
def normalize_phone(raw: str) -> Optional[str]:
    if not raw:
        return None
    digits = "".join(ch for ch in raw if ch.isdigit())
    # espera algo como 55DDDNXXXXXXXX (padrão Z-API)
    if digits.startswith("55"):
        return digits
    # se vier DDD+numero, prefixa 55
    if len(digits) >= 10:
        return "55" + digits
    return None

async def send_whatsapp(phone: str, message: str) -> Dict[str, Any]:
    headers = {"Client-Token": ZAPI_CLIENT_TOKEN}
    payload = {"phone": phone, "message": message}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(ZAPI_URL, headers=headers, json=payload)
        return {"status": r.status_code, "body": r.text}

def build_message(name, product, price, url):
    return MSG_TEMPLATE.format(
        name=name or "tudo bem",
        product=product or "seu eBook",
        price=price or "o melhor preço",
        checkout_url=url or "",
        brand=SENDER_NAME
    )

def parse_cartpanda_payload(payload: dict) -> dict:
    """
    Ajuste aqui se seus campos forem diferentes.
    Dica: veja nos LOGS do Render o JSON real que está chegando.
    """
    # tenta achar campos comuns
    order = payload.get("order") or payload.get("data") or payload
    customer = order.get("customer", {})
    items = order.get("items") or order.get("line_items") or []

    order_id = str(order.get("id") or order.get("order_id") or payload.get("id") or "")
    status = (order.get("status") or payload.get("event") or "").lower()
    payment_status = (order.get("payment_status") or order.get("financial_status") or "").lower()
    payment_method = (order.get("payment_method") or "").lower()

    name = customer.get("name") or customer.get("first_name")
    phone = normalize_phone(customer.get("phone") or customer.get("whatsapp") or "")
    price = None
    product = None
    if items:
        first = items[0]
        product = first.get("title") or first.get("name")
        price = str(first.get("price") or first.get("amount") or "")

    checkout_url = order.get("checkout_url") or order.get("abandoned_checkout_url") or ""
    return dict(
        order_id=order_id, status=status, payment_status=payment_status,
        payment_method=payment_method, name=name, phone=phone,
        product=product, price=price, checkout_url=checkout_url
    )

# ---------- Lógica ----------
async def schedule_abandoned_followup(info: dict):
    """
    Agenda o envio para +5min. Se chegar 'paid' antes, cancelamos.
    """
    order_id = info["order_id"]
    if not order_id:
        log.warning("Sem order_id; não agendado.")
        return

    # se já houver um agendamento para este pedido, cancela e recria
    old = PENDING_JOBS.get(order_id)
    if old:
        try:
            scheduler.remove_job(old)
        except Exception:
            pass

    run_at = datetime.utcnow() + timedelta(minutes=5)
    job = scheduler.add_job(
        send_abandoned_message,
        trigger=DateTrigger(run_date=run_at),
        kwargs={"info": info},
        id=f"abandoned-{order_id}",
        replace_existing=True,
        misfire_grace_time=180  # tolerância
    )
    PENDING_JOBS[order_id] = job.id
    log.info(f"[{order_id}] agendado para {run_at} UTC")

async def send_abandoned_message(info: dict):
    """
    Envia WhatsApp se ainda não foi pago.
    """
    order_id = info["order_id"]
    phone = info["phone"]
    if not phone:
        log.warning(f"[{order_id}] Sem telefone; não enviou.")
        return

    # Segurança extra: se por acaso marcamos como pago, aborta
    if order_id not in PENDING_JOBS:
        log.info(f"[{order_id}] job inexistente (provável pago). Abortado.")
        return

    message = build_message(info["name"], info["product"], info["price"], info["checkout_url"])
    resp = await send_whatsapp(phone, message)
    log.info(f"[{order_id}] WhatsApp status={resp['status']} body={resp['body']}")
    # concluiu: remove job
    PENDING_JOBS.pop(order_id, None)

def cancel_if_paid(order_id: str):
    job_id = PENDING_JOBS.pop(order_id, None)
    if job_id:
        try:
            scheduler.remove_job(job_id)
            log.info(f"[{order_id}] pagamento confirmado -> job cancelado.")
        except Exception:
            pass

# ---------- Webhook ----------
@app.post("/webhook/cartpanda")
async def cartpanda_webhook(request: Request):
    payload = await request.json()
    log.info(f"Webhook recebido: {payload}")
    info = parse_cartpanda_payload(payload)

    # Heurística de eventos (ajuste aos seus nomes reais de evento/status):
    event = (payload.get("event") or info["status"] or "").lower()

    if "paid" in event or info["payment_status"] in {"paid", "pago"}:
        cancel_if_paid(info["order_id"])
        return JSONResponse({"ok": True, "action": "paid_cancelled", "order_id": info["order_id"]})

    # Abandono / pedido criado / pix pendente:
    if any(k in event for k in ["checkout.abandoned", "order.created", "pending", "pix"]):
        await schedule_abandoned_followup(info)
        return JSONResponse({"ok": True, "action": "scheduled_5min", "order_id": info["order_id"]})

    # fallback: apenas loga
    return JSONResponse({"ok": True, "action": "ignored_or_unknown", "event": event, "parsed": info})

