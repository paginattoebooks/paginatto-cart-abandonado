# main.py — Carrinho Abandonado → WhatsApp (Z-API)
import os
import logging
from typing import Any, Dict, Optional, List

from fastapi import FastAPI, Body
from fastapi.responses import JSONResponse
import httpx

# ---------------- Logging ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("paginatto-abandoned")

# ---------------- Env Vars ----------------
ZAPI_INSTANCE: str = os.getenv("ZAPI_INSTANCE", "")
ZAPI_TOKEN: str = os.getenv("ZAPI_TOKEN", "")
ZAPI_CLIENT_TOKEN: str = os.getenv("ZAPI_CLIENT_TOKEN", "")
SENDER_NAME: str = os.getenv("WHATSAPP_SENDER_NAME", "Paginatto")

MSG_TEMPLATE: Optional[str] = os.getenv(
    "MSG_TEMPLATE",
    (
        "Te achei {first_name}! Você deixou {product} no carrinho por {price}.\n"
        "Finalize aqui: {checkout_url}\n— {brand}"
    ),
)

ZAPI_URL: str = (
    f"https://api.z-api.io/instances/{ZAPI_INSTANCE}/token/{ZAPI_TOKEN}/send-text"
)

app = FastAPI(title="Paginatto - Carrinho Abandonado", version="1.1.0")


# ---------------- Helpers ----------------
def normalize_phone(raw: Optional[str]) -> Optional[str]:
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
        try:
            body = r.json()
        except Exception:
            body = r.text
        return {"status": r.status_code, "body": body}


class _SafeDict(dict):
    def __missing__(self, key):  # evita KeyError em templates
        return ""


def safe_format(template: Optional[str], data: Dict[str, Any]) -> str:
    template = template or ""
    return template.format_map(_SafeDict(data))


def currency_brl(value: Any) -> str:
    try:
        v = float(value)
        return f"R$ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except Exception:
        return "R$ 0,00"


def _coalesce(values: List[Any]) -> Optional[Any]:
    for v in values:
        if v not in (None, "", [], {}):
            return v
    return None


def resolve_checkout_url(payload: dict, scoped: Optional[dict] = None) -> str:
    s = scoped or {}
    keys = [
        "checkout_link",
        "checkout_url",
        "cart_url",
        "recovery_url",
        "recover_url",
        "abandoned_checkout_url",
    ]
    for k in keys:
        if s.get(k):
            return s[k]
    # procurar no payload bruto
    for k in keys:
        if payload.get(k):
            return payload[k]
    if payload.get("data"):
        for k in keys:
            if payload["data"].get(k):
                return payload["data"][k]
    return ""


def _product_from_item(item: dict) -> str:
    # tenta várias formas comuns
    variant = item.get("variant") or {}
    prod_obj = item.get("product") or variant.get("product") or {}
    title = _coalesce(
        [
            item.get("name"),
            item.get("title"),
            prod_obj.get("title"),
            " ".join(
                t
                for t in [
                    item.get("title"),
                    item.get("variant_title") or variant.get("title"),
                ]
                if t
            ),
        ]
    )
    return title or "Seu produto"


def _price_from_item_or_totals(item: dict, totals: List[Any]) -> str:
    raw = _coalesce(
        [
            item.get("price"),
            item.get("unit_price"),
            item.get("line_price"),
            item.get("subtotal"),
            item.get("total"),
        ]
    )
    if raw in (None, "", 0):
        raw = _coalesce(totals)
    return currency_brl(raw)


# ---------------- Parsers ----------------
def parse_abandoned_payload(payload: dict) -> dict:
    data = payload.get("data", {}) or {}
    cust = data.get("customer") or data.get("customer_info") or {}
    items = data.get("cart_line_items") or data.get("line_items") or data.get("items") or []

    # nome
    name = (
        cust.get("full_name")
        or f"{cust.get('first_name','')} {cust.get('last_name','')}".strip()
        or "cliente"
    )
    first_name = cust.get("first_name") or (name.split()[0] if name else "cliente")

    # item + preço
    first = (items[0] or {}) if items else {}
    product = _product_from_item(first)
    price = _price_from_item_or_totals(
        first,
        [
            data.get("total_line_items_price"),
            data.get("subtotal_price"),
            data.get("total_price"),
        ],
    )

    checkout_url = resolve_checkout_url(payload, data)

    return {
        "order_id": data.get("id"),
        "event": (payload.get("event") or "checkout.abandoned"),
        "name": name,
        "first_name": first_name,
        "phone": cust.get("phone"),
        "product": product,
        "price": price,
        "checkout_url": checkout_url,
        "cart_url": checkout_url,
    }


def parse_order_payload(payload: dict) -> dict:
    order = payload.get("order", {}) or {}
    customer = order.get("customer", {}) or {}
    items = order.get("line_items") or order.get("items") or [{}]

    name = (
        customer.get("name")
        or customer.get("full_name")
        or f"{customer.get('first_name','')} {customer.get('last_name','')}".strip()
        or "cliente"
    )
    first_name = customer.get("first_name") or (name.split()[0] if name else "cliente")

    first = items[0] or {}
    product = _product_from_item(first)
    price = _price_from_item_or_totals(
        first,
        [
            order.get("total_line_items_price"),
            order.get("subtotal_price"),
            order.get("total_price"),
            order.get("unformatted_total_price"),
        ],
    )

    checkout_url = resolve_checkout_url(payload, order)

    return {
        "order_id": order.get("id"),
        "event": (payload.get("event") or order.get("status") or "order.updated"),
        "name": name,
        "first_name": first_name,
        "phone": customer.get("phone"),
        "product": product,
        "price": price,
        "checkout_url": checkout_url,
        "cart_url": checkout_url,
    }


def parse_cartpanda_payload(payload: dict) -> dict:
    event = (payload.get("event") or "").lower()
    if "abandoned" in event or ("data" in payload and payload["data"]):
        return parse_abandoned_payload(payload)
    if "order" in payload:
        return parse_order_payload(payload)
    # fallback: tenta como pedido
    return parse_order_payload(payload)


# ---------------- Webhook ----------------
@app.post("/webhook/cartpanda")
async def cartpanda_webhook(payload: Dict[str, Any] = Body(...)):
    log.info("Webhook recebido")
    info = parse_cartpanda_payload(payload)
    event = (payload.get("event") or info.get("event") or "").lower()

    if "abandoned" not in event and "checkout.abandoned" not in event:
        log.info(f"[{info.get('order_id')}] ignorado event={event}")
        return JSONResponse({"ok": True, "action": "ignored", "event": event, "order_id": info.get("order_id")})

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


# ---------------- Health/Root ----------------
@app.get("/")
def root():
    return {"ok": True, "service": "paginatto-abandoned"}

@app.get("/health")
def health():
    return {"ok": True}


# Execução local (opcional)
if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", "10000")))

