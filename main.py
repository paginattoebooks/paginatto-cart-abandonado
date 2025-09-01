import os, logging
from typing import Dict, Any, Optional
import httpx
from fastapi import FastAPI, Body
from fastapi.responses import JSONResponse

# --- Log básico ---
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("paginatto")

# --- Config via variáveis de ambiente (Render -> Settings -> Environment) ---
ZAPI_INSTANCE = os.getenv("ZAPI_INSTANCE", "")
ZAPI_TOKEN = os.getenv("ZAPI_TOKEN", "")
ZAPI_CLIENT_TOKEN = os.getenv("ZAPI_CLIENT_TOKEN", "")
SENDER_NAME = os.getenv("WHATSAPP_SENDER_NAME", "Paginatto")
MSG_TEMPLATE = os.getenv(
    "MSG_TEMPLATE",
    "Oi {name}! Você deixou {product} no carrinho da {brand} por {price}. "
    "Finalize aqui: {checkout_url}"
)

ZAPI_URL = f"https://api.z-api.io/instances/{ZAPI_INSTANCE}/token/{ZAPI_TOKEN}/send-text"

app = FastAPI(title="Paginatto - Carrinho Abandonado", version="0.1.0")


# ---------- Helpers ----------
def normalize_phone(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    digits = "".join(ch for ch in raw if ch.isdigit())
    if digits.startswith("55"):
        return digits
    if len(digits) >= 10:  # DDD + número
        return "55" + digits
    return None

def build_message(name, product, price, url):
    return MSG_TEMPLATE.format(
        name=name or "tudo bem",
        product=product or "seu eBook",
        price=price or "um ótimo preço",
        checkout_url=url or "",
        brand=SENDER_NAME
    )

async def send_whatsapp(phone: str, message: str) -> Dict[str, Any]:
    headers = {"Client-Token": ZAPI_CLIENT_TOKEN}
    payload = {"phone": phone, "message": message}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(ZAPI_URL, headers=headers, json=payload)
        return {"status": r.status_code, "body": r.text}

def parse_cartpanda_payload(payload: dict) -> dict:
    """
    Ajuste aqui conforme o JSON do seu CartPanda (olhe os logs).
    """
    order = payload.get("order") or payload.get("data") or payload
    customer = order.get("customer", {})
    items = order.get("items") or order.get("line_items") or []

    order_id = str(order.get("id") or order.get("order_id") or payload.get("id") or "")
    status = (payload.get("event") or order.get("status") or "").lower()
    payment_status = (order.get("payment_status") or order.get("financial_status") or "").lower()
    payment_method = (order.get("payment_method") or "").lower()

    name = customer.get("name") or customer.get("first_name")
    phone = normalize_phone(customer.get("phone") or customer.get("whatsapp"))
    product = items[0].get("title") if items else None
    price = str(items[0].get("price")) if (items and items[0].get("price") is not None) else None
    checkout_url = order.get("checkout_url") or order.get("abandoned_checkout_url") or ""

    return dict(
        order_id=order_id,
        status=status,
        payment_status=payment_status,
        payment_method=payment_method,
        name=name,
        phone=phone,
        product=product,
        price=price,
        checkout_url=checkout_url,
    )


# ---------- Rotas ----------
@app.get("/health")
def health():
    return {"ok": True, "service": "paginatto", "version": "0.1.0"}

@app.post("/webhook/cartpanda")
async def cartpanda_webhook(payload: Dict[str, Any] = Body(...)):
    log.info(f"Webhook recebido: {payload}")
    info = parse_cartpanda_payload(payload)

    event = (payload.get("event") or info["status"] or "").lower()

    # Dispara IMEDIATO quando o CartPanda mandar "carrinho abandonado"
    if any(k in event for k in ["checkout.abandoned", "carrinho", "abandonado"]):
        if not info.get("phone"):
            log.warning(f"[{info.get('order_id')}] Sem telefone válido. Abortado.")
            return JSONResponse({"ok": True, "action": "skipped_no_phone", "order_id": info.get("order_id")})

        message = build_message(info["name"], info["product"], info["price"], info["checkout_url"])
        resp = await send_whatsapp(info["phone"], message)
        log.info(f"[{info.get('order_id')}] WhatsApp status={resp['status']} body={resp['body']}")
        return JSONResponse({"ok": True, "action": "sent_immediately", "order_id": info.get("order_id")})

    # Outros eventos: só registramos
    return JSONResponse({"ok": True, "action": "ignored_or_unknown", "event": event})





