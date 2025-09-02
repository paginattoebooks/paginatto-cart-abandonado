import os
import logging
from typing import Dict, Any
from fastapi import FastAPI, Body
from fastapi.responses import JSONResponse
import httpx

# =========================
# Log
# =========================
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("paginatto")

# =========================
# Config (Render → Settings → Environment)
# =========================
ZAPI_INSTANCE = os.getenv("ZAPI_INSTANCE", "")
ZAPI_TOKEN = os.getenv("ZAPI_TOKEN", "")
ZAPI_CLIENT_TOKEN = os.getenv("ZAPI_CLIENT_TOKEN", "")
SENDER_NAME = os.getenv("WHATSAPP_SENDER_NAME", "Paginatto")
MSG_TEMPLATE = os.getenv(
    "MSG_TEMPLATE",
    (
        "Oi {name}! Sou Iara, consultora de vendas da Paginatto e vi que você deixou "
        "o produto {product} no carrinho por {price}. Finalize aqui: {checkout_url}"
    ),
)

ZAPI_URL = f"https://api.z-api.io/instances/{ZAPI_INSTANCE}/token/{ZAPI_TOKEN}/send-text"

if not all([ZAPI_INSTANCE, ZAPI_TOKEN, ZAPI_CLIENT_TOKEN]):
    log.warning(
        "⚠️ Z-API env vars ausentes: verifique ZAPI_INSTANCE, ZAPI_TOKEN, ZAPI_CLIENT_TOKEN em Settings → Environment."
    )

# =========================
# App
# =========================
app = FastAPI(title="Paginatto - Carrinho Abandonado", version="1.1.0")

# =========================
# Helpers
# =========================
def normalize_phone(raw: str | None) -> str | None:
    """Mantém apenas dígitos e garante DDI 55."""
    if not raw:
        return None
    digits = "".join(ch for ch in raw if ch.isdigit())
    if digits.startswith("55"):
        return digits
    if len(digits) >= 10:
        return "55" + digits
    return None


async def send_whatsapp(phone: str, message: str) -> Dict[str, Any]:
    headers = {"Client-Token": ZAPI_CLIENT_TOKEN}
    payload = {"phone": phone, "message": message}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(ZAPI_URL, headers=headers, json=payload)
        return {"status": r.status_code, "body": r.text}


def parse_cartpanda_payload(payload: dict) -> dict:
    """Extrai campos relevantes do webhook do CartPanda."""
    order = payload.get("order", {}) or {}
    customer = order.get("customer", {}) or {}
    items = order.get("items") or [{}]

    return {
        "order_id": order.get("id"),
        "status": order.get("status"),
        "payment_status": order.get("payment_status"),
        "payment_method": order.get("payment_method"),
        "checkout_url": order.get("checkout_url"),
        "name": customer.get("name"),
        "phone": customer.get("phone"),
        "product": items[0].get("title", "Produto"),
        "price": items[0].get("price", "R$ 0,00"),
    }

# =========================
# Routes
# =========================
@app.get("/health")
async def health():
    return {"ok": True, "serviço": "paginatto", "versão": "1.1.0"}


@app.post("/webhook/cartpanda")
async def cartpanda_webhook(payload: Dict[str, Any] = Body(...)):
    log.info(f"Webhook recebido: {payload}")
    info = parse_cartpanda_payload(payload)

    event = (payload.get("event") or info.get("status") or "").lower()

    # Só trata carrinho abandonado
    if "checkout.abandoned" in event:
        phone = normalize_phone(info.get("phone"))
        log.info(f"[{info.get('order_id')}] phone_normalized={phone}")

        if not phone:
            log.warning(f"[{info.get('order_id')}] telefone inválido -> não enviou.")
            return JSONResponse({"ok": False, "error": "telefone inválido"})

        message = MSG_TEMPLATE.format(
            name=info.get("name", "cliente"),
            product=info.get("product", "seu produto"),
            price=info.get("price", "R$ 0,00"),
            checkout_url=info.get("checkout_url", "#"),
            brand=SENDER_NAME,
        )

        result = await send_whatsapp(phone, message)
        log.info(f"[{info.get('order_id')}] WhatsApp enviado -> {result}")

        return JSONResponse(
            {"ok": True, "action": "whatsapp_sent", "order_id": info.get("order_id")}
        )

    # Outros eventos são ignorados
    return JSONResponse(
        {"ok": True, "action": "ignored", "event": event, "order_id": info.get("order_id")}
    )
