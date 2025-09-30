# main.py
import os
import logging
from typing import Dict, Any, Optional

from fastapi import FastAPI, Body, Request
from fastapi.responses import JSONResponse
import httpx

# ---------------- Log ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("paginatto")

# --------- Variáveis de ambiente (Render → Settings → Environment) ---------
ZAPI_INSTANCE: str = os.getenv("ZAPI_INSTANCE", "")
ZAPI_TOKEN: str = os.getenv("ZAPI_TOKEN", "")
ZAPI_CLIENT_TOKEN: str = os.getenv("ZAPI_CLIENT_TOKEN", "")
SENDER_NAME: str = os.getenv("WHATSAPP_SENDER_NAME", "Paginatto")
MSG_TEMPLATE: Optional[str] = os.getenv("MSG_TEMPLATE")

ZAPI_URL: str = f"https://api.z-api.io/instances/{ZAPI_INSTANCE}/token/{ZAPI_TOKEN}/send-text"

# ---------------- App ----------------
app = FastAPI(title="Paginatto - Carrinho Abandonado", version="1.0.0")


# ---------------- Helpers ----------------
def normalize_phone(raw: Optional[str]) -> Optional[str]:
    """Remove caracteres e garante padrão 55DDDNXXXXXXXX."""
    if not raw:
        return None
    digits = "".join(ch for ch in str(raw) if ch.isdigit())
    if not digits:
        return None
    if digits.startswith("55"):
        return digits
    if len(digits) >= 10:
        return "55" + digits
    return None


async def zapi_send_text(phone: str, message: str) -> Dict[str, Any]:
    headers = {"Client-Token": ZAPI_CLIENT_TOKEN, "Content-Type": "application/json"}
    payload = {"phone": phone, "message": message}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(ZAPI_URL, headers=headers, json=payload)
        body = None
        try:
            body = r.json()
        except Exception:
            body = r.text
        return {"status": r.status_code, "body": body}


class _SafeDict(dict):
    def __missing__(self, key):
        return ""


def safe_format(template: Optional[str], data: Dict[str, Any]) -> str:
    """Formata sem KeyError se faltar variável no template."""
    if not template:
        template = "Te achei {name}! Você deixou {product} no carrinho.\nLink: {checkout_url}"
    return template.format_map(_SafeDict(data))


def currency_brl(value: Any) -> str:
    try:
        v = float(value)
        return f"R$ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except Exception:
        return str(value) if value else "R$ 0,00"


def resolve_checkout_url(payload: dict, scoped: Optional[dict] = None) -> str:
    scoped = scoped or {}
    return (
        scoped.get("checkout_link")
        or scoped.get("checkout_url")
        or scoped.get("cart_url")
        or payload.get("checkout_url")
        or payload.get("cart_url")
        or payload.get("data", {}).get("checkout_url")
        or payload.get("data", {}).get("cart_url")
        or ""
    )


# ---------------- Parsers ----------------
def parse_abandoned_payload(payload: dict) -> dict:
    """Evento CartPanda: abandoned.created (em payload['data'])."""
    data = payload.get("data", {}) or {}
    cust = data.get("customer") or data.get("customer_info") or {}
    items = data.get("cart_line_items") or []

    # Nome
    name = (
        cust.get("full_name")
        or f"{cust.get('first_name', '')} {cust.get('last_name', '')}".strip()
        or "cliente"
    )
    first_name = cust.get("first_name") or (name.split()[0] if name else "cliente")

    # Telefone
    phone = cust.get("phone")

    # Produto / Preço
    product = ""
    price = None
    if items:
        first = items[0] or {}
        variant = first.get("variant") or {}
        product = (variant.get("product") or {}).get("title") or variant.get("title") or ""
        price = variant.get("price")

    if price in (None, "", 0) and data.get("total_line_items_price") is not None:
        price = data.get("total_line_items_price")

    checkout_url = resolve_checkout_url(payload, data)

    return {
        "order_id": data.get("id"),
        "event": "checkout.abandoned",
        "name": name,
        "first_name": first_name,
        "phone": phone,
        "product": product or "Seu produto",
        "price": currency_brl(price),
        "checkout_url": checkout_url,
        "cart_url": checkout_url,
    }


def parse_order_payload(payload: dict) -> dict:
    """Formato de pedido (payload['order'])."""
    order = payload.get("order", {}) or {}
    customer = order.get("customer", {}) or {}
    items = order.get("items", [{}]) or [{}]

    name = (
        customer.get("name")
        or customer.get("full_name")
        or f"{customer.get('first_name','')} {customer.get('last_name','')}".strip()
        or "cliente"
    )
    first_name = customer.get("first_name") or (name.split()[0] if name else "cliente")

    checkout_url = resolve_checkout_url(payload, order)

    return {
        "order_id": order.get("id"),
        "event": order.get("status") or "order.updated",
        "name": name,
        "first_name": first_name,
        "phone": customer.get("phone"),
        "product": (items[0] or {}).get("title", "Produto"),
        "price": currency_brl((items[0] or {}).get("price")),
        "checkout_url": checkout_url,
        "cart_url": checkout_url,
    }


def parse_cartpanda_payload(payload: dict) -> dict:
    """Detecta e extrai de ambos formatos (abandoned.created ou order)."""
    if "data" in payload:
        return parse_abandoned_payload(payload)
    return parse_order_payload(payload)


# ---------------- Webhooks ----------------
@app.post("/webhook/cartpanda")
async def cartpanda_webhook(payload: Dict[str, Any] = Body(...)):
    log.info("Webhook recebido")
    info = parse_cartpanda_payload(payload)
    event = (payload.get("event") or info.get("event") or "").lower()

    if "abandoned" in event or "checkout.abandoned" in event:
        phone_norm = normalize_phone(info.get("phone"))
        log.info(f"[{info.get('order_id')}] phone_raw={info.get('phone')} phone_norm={phone_norm}")

        if not phone_norm:
            log.warning(f"[{info.get('order_id')}] telefone inválido -> não enviou")
            return JSONResponse({"ok": False, "error": "telefone inválido", "order_id": info.get("order_id")})

        data = {
            "name": info.get("name", "cliente"),
            "first_name": info.get("first_name", "cliente"),
            "product": info.get("product", "seu produto"),
            "price": info.get("price", "R$ 0,00"),
            "checkout_url": info.get("checkout_url", ""),
            "cart_url": info.get("cart_url", ""),
            "brand": SENDER_NAME,
        }
        message = safe_format(MSG_TEMPLATE, data)

        result = await zapi_send_text(phone_norm, message)
        log.info(f"[{info.get('order_id')}] WhatsApp -> {result}")
        return JSONResponse({"ok": True, "action": "whatsapp_sent", "order_id": info.get("order_id")})

    log.info(f"[{info.get('order_id')}] ignorado event={event}")
    return JSONResponse({"ok": True, "action": "ignored", "event": event, "order_id": info.get("order_id")})


# ---------------- Health ----------------
@app.get("/health")
async def health():
    return {"ok": True, "servico": "paginatto", "versao": "1.0.0"}


# Execução local opcional
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
