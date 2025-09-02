import os
import logging
from typing import Dict, Any, Optional
from fastapi import FastAPI, Body
from fastapi.responses import JSONResponse
import httpx

# ---------------- Log ----------------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("paginatto")

# --------- Variáveis de ambiente (Render → Settings → Environment) ---------
ZAPI_INSTANCE: str = os.getenv("ZAPI_INSTANCE", "")
ZAPI_TOKEN: str = os.getenv("ZAPI_TOKEN", "")
ZAPI_CLIENT_TOKEN: str = os.getenv("ZAPI_CLIENT_TOKEN", "")
SENDER_NAME: str = os.getenv("WHATSAPP_SENDER_NAME", "Paginatto")

MSG_TEMPLATE: str = os.getenv(
    "MSG_TEMPLATE",
    (
        "Oi {name}! Sou Iara, consultora de vendas da {brand} e vi que você deixou o produto "
        "{product} no carrinho por {price}. Finalize aqui: {checkout_url}"
    ),
)

ZAPI_URL: str = f"https://api.z-api.io/instances/{ZAPI_INSTANCE}/token/{ZAPI_TOKEN}/send-text"

# ---------------- App ----------------
app = FastAPI(title="Paginatto - Carrinho Abandonado", version="1.0.0")


# ---------------- Helpers ----------------
def normalize_phone(raw: Optional[str]) -> Optional[str]:
    """Remove caracteres e garante padrão 55DDDNXXXXXXXX."""
    if not raw:
        return None
    digits = "".join(ch for ch in raw if ch.isdigit())
    if not digits:
        return None
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


# ---------------- Parsers ----------------
def parse_abandoned_payload(payload: dict) -> dict:
    """Extrai dados quando o evento é abandoned.created (dados em payload['data'])."""
    data = payload.get("data", {}) or {}
    cust = data.get("customer") or data.get("customer_info") or {}
    items = data.get("cart_line_items") or []

    # Nome
    name = cust.get("full_name") or f"{cust.get('first_name', '')} {cust.get('last_name', '')}".strip() or "cliente"

    # Telefone
    phone = cust.get("phone")

    # Produto / Preço
    product = None
    price = None
    if items:
        first = items[0] or {}
        variant = first.get("variant") or {}
        product = (variant.get("product") or {}).get("title") or variant.get("title")
        price = variant.get("price")  # pode vir float

    # Fallback para total do carrinho
    if price in (None, "", 0) and data.get("total_line_items_price") is not None:
        price = data.get("total_line_items_price")

    # Formata preço
    if isinstance(price, (int, float)):
        price = f"R$ {price:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

    return {
        "order_id": data.get("id"),
        "status": "checkout.abandoned",
        "payment_status": None,
        "payment_method": None,
        "checkout_url": data.get("cart_url"),
        "name": name,
        "phone": phone,
        "product": product or "Seu produto",
        "price": price or "R$ 0,00",
    }


def parse_order_payload(payload: dict) -> dict:
    """Extrai dados quando o formato é de pedido (dados em payload['order'])."""
    order = payload.get("order", {}) or {}
    customer = order.get("customer", {}) or {}
    items = order.get("items", [{}]) or [{}]

    return {
        "order_id": order.get("id"),
        "status": order.get("status"),
        "payment_status": order.get("payment_status"),
        "payment_method": order.get("payment_method"),
        "checkout_url": order.get("checkout_url"),
        "name": customer.get("name") or customer.get("full_name") or customer.get("first_name") or "cliente",
        "phone": customer.get("phone"),
        "product": items[0].get("title", "Produto"),
        "price": items[0].get("price", "R$ 0,00"),
    }


def parse_cartpanda_payload(payload: dict) -> dict:
    """Detecta e extrai de ambos formatos (abandoned.created ou pedido)."""
    if "data" in payload:  # abandoned.created
        return parse_abandoned_payload(payload)
    return parse_order_payload(payload)


# ---------------- Webhook ----------------
@app.post("/webhook/cartpanda")
async def cartpanda_webhook(payload: Dict[str, Any] = Body(...)):
    log.info(f"Webhook recebido: {payload}")

    info = parse_cartpanda_payload(payload)
    event = (payload.get("event") or info.get("status") or "").lower()

    # Dispara para carrinho abandonado
    if ("abandoned" in event) or ("checkout.abandoned" in event):
        phone_norm = normalize_phone(info.get("phone"))
        log.info(f"[{info.get('order_id')}] phone_raw={info.get('phone')} phone_norm={phone_norm}")

        if not phone_norm:
            log.warning(f"[{info.get('order_id')}] telefone inválido -> não enviou")
            return JSONResponse({"ok": False, "error": "telefone inválido", "order_id": info.get("order_id")})

        message = MSG_TEMPLATE.format(
            name=info.get("name", "cliente"),
            product=info.get("product", "seu produto"),
            price=info.get("price", "R$ 0,00"),
            checkout_url=info.get("checkout_url", "#"),
            brand=SENDER_NAME,
        )

        result = await send_whatsapp(phone_norm, message)
        log.info(f"[{info.get('order_id')}] WhatsApp -> {result}")
        return JSONResponse({"ok": True, "action": "whatsapp_sent", "order_id": info.get("order_id")})

    # Outros eventos: só loga
    log.info(f"[{info.get('order_id')}] ignorado event={event}")
    return JSONResponse({"ok": True, "action": "ignored", "event": event, "order_id": info.get("order_id")})


# ---------------- Health ----------------
@app.get("/health")
async def health():
    return {"ok": True, "serviço": "paginatto", "versão": "1.0.0"}

