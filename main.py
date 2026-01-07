# main.py — Carrinho Abandonado → WhatsApp (UAZAPI) [Render-ready]
import os
import logging
from typing import Any, Dict, Optional, List, Set

from fastapi import FastAPI, Body
from fastapi.responses import JSONResponse, Response
import httpx

# ---------------- Logging ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("paginatto-abandoned")

# ---------------- Env Vars (UAZAPI) ----------------
# URL COMPLETA (isso elimina 100% a treta de /send/text no código)
UAZAPI_SEND_URL: str = os.getenv("UAZAPI_SEND_URL", "").strip()

# Token da INSTÂNCIA (igual o teste que funcionou: header "token")
UAZAPI_TOKEN: str = os.getenv("UAZAPI_TOKEN", "").strip()

SENDER_NAME: str = os.getenv("WHATSAPP_SENDER_NAME", "Paginatto")

MSG_TEMPLATE: str = os.getenv(
    "MSG_TEMPLATE",
    (
        "Te achei {first_name}! Você deixou {product} no carrinho por {price}.\n"
        "Finalize aqui: {checkout_url}\n— {brand}"
    ),
)

# HTTP client settings
HTTP_TIMEOUT_SECONDS = float(os.getenv("HTTP_TIMEOUT_SECONDS", "30"))

app = FastAPI(title="Paginatto - Carrinho Abandonado", version="3.0.0")

# simples idempotência em memória (evita mandar várias vezes em re-tentativas do CartPanda)
sent_orders: Set[str] = set()
SENT_ORDERS_MAX = int(os.getenv("SENT_ORDERS_MAX", "5000"))


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


class _SafeDict(dict):
    def __missing__(self, key):
        return ""


def safe_format(template: str, data: Dict[str, Any]) -> str:
    return (template or "").format_map(_SafeDict(data))


def currency_brl(value: Any) -> str:
    try:
        v = float(str(value).replace(",", "."))
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
    for k in keys:
        if payload.get(k):
            return payload[k]
    if payload.get("data"):
        for k in keys:
            if payload["data"].get(k):
                return payload["data"][k]
    return ""


def _product_from_item(item: dict) -> str:
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


async def uazapi_send_text(number_e164: str, text: str) -> Dict[str, Any]:
    """
    Implementação idêntica ao "Experimente!" do painel:
      POST {UAZAPI_SEND_URL}
      headers: token, Accept, Content-Type
      body: {"number": "...", "text": "..."}
    """
    if not UAZAPI_SEND_URL:
        return {"ok": False, "status": "error", "error": "missing_env:UAZAPI_SEND_URL"}
    if not UAZAPI_TOKEN:
        return {"ok": False, "status": "error", "error": "missing_env:UAZAPI_TOKEN"}

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "token": UAZAPI_TOKEN,
    }
    payload = {"number": number_e164, "text": text}

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
            r = await client.post(UAZAPI_SEND_URL, headers=headers, json=payload)

        try:
            body = r.json()
        except Exception:
            body = r.text

        return {"ok": (200 <= r.status_code < 300), "status": r.status_code, "body": body}
    except Exception as e:
        log.exception(f"Erro HTTP ao enviar WhatsApp (UAZAPI): {e}")
        return {"ok": False, "status": "error", "error": str(e)}


# ---------------- Parsers (CartPanda) ----------------
def parse_abandoned_payload(payload: dict) -> dict:
    data = payload.get("data", {}) or {}
    cust = data.get("customer") or data.get("customer_info") or {}
    items = data.get("cart_line_items") or data.get("line_items") or data.get("items") or []

    name = (
        cust.get("full_name")
        or f"{cust.get('first_name','')} {cust.get('last_name','')}".strip()
        or "cliente"
    )
    first_name = cust.get("first_name") or (name.split()[0] if name else "cliente")

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
    return parse_order_payload(payload)


# ---------------- Webhook ----------------
@app.post("/webhook/cartpanda")
async def cartpanda_webhook(payload: Dict[str, Any] = Body(...)):
    log.info("Webhook recebido")
    info = parse_cartpanda_payload(payload)
    event = (payload.get("event") or info.get("event") or "").lower()

    # só processa eventos de abandono
    if "abandoned" not in event and "checkout.abandoned" not in event:
        log.info(f"[{info.get('order_id')}] ignorado event={event}")
        return JSONResponse({"ok": True, "action": "ignored", "event": event, "order_id": info.get("order_id")})

    order_id = str(info.get("order_id") or "")
    if order_id:
        if order_id in sent_orders:
            log.info(f"[{order_id}] já enviado antes -> ignore (idempotência)")
            return JSONResponse({"ok": True, "action": "already_sent", "order_id": order_id})
        # controle de tamanho do set
        if len(sent_orders) >= SENT_ORDERS_MAX:
            sent_orders.clear()

    phone_norm = normalize_phone(info.get("phone"))
    log.info(f"[{order_id}] phone_raw={info.get('phone')} phone_norm={phone_norm}")

    if not phone_norm:
        log.warning(f"[{order_id}] telefone inválido -> não enviou")
        return JSONResponse({"ok": False, "error": "telefone inválido", "order_id": order_id})

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

    result = await uazapi_send_text(phone_norm, message)
    log.info(f"[{order_id}] WhatsApp -> {result}")

    if result.get("ok") and order_id:
        sent_orders.add(order_id)

    return JSONResponse(
        {
            "ok": bool(result.get("ok")),
            "action": "whatsapp_sent" if result.get("ok") else "whatsapp_failed",
            "order_id": order_id,
            "provider": "uazapi",
            "result": result,
        }
    )


# ---------------- Root/Health/Favicon ----------------
@app.get("/")
def root():
    return {"ok": True, "service": "paginatto-abandoned", "provider": "uazapi"}

@app.get("/health")
def health():
    return {"ok": True}

@app.get("/favicon.ico")
@app.get("/favicon.png")
def favicon():
    return Response(status_code=204)


# Execução local (opcional)
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", "10000")))



