"""Reglas para detectar precios sospechosamente bajos.

Solo se evalúan productos nuevos o que cambiaron en esta corrida, y un hallazgo se avisa una
vez mientras dure (ver db.save_alert), así no se repite cada 15 minutos ni con cada reajuste
chico de precio.
"""

import bisect
import re
import time
from urllib.parse import quote

from . import db

DEFAULT_RULES = {
    "precio_absurdo": 10,
    "caida_vs_historial": 0.5,
    "descuento_extremo": 0.2,
    "vs_otras_tiendas": 0.5,
    "gran_descuento": 0.5,
    "nuevo_vs_pasillo": 0.2,
    "historial_dias": 14,
    "realertar_si_baja": 0.05,
    # Clasificación de descuentos (ver offer_status)
    "oferta_permanente_dias": 7,
    "oferta_min_historial_dias": 7,
    "oferta_real_baja": 0.15,
}
SAME_PRICE = 0.02  # reajustes de ±2 % cuentan como el mismo precio
MIN_COMPARABLES = 8  # productos del mismo sub-pasillo y unidad para comparar precio por unidad

_UNITS = {"g": ("g", 1), "gr": ("g", 1), "kg": ("g", 1000),
          "ml": ("ml", 1), "cc": ("ml", 1), "l": ("ml", 1000), "lt": ("ml", 1000)}
_PRESENTATION = re.compile(r"^\s*(?:(\d+)\s*[xX]\s*)?(\d+(?:[.,]\d+)?)\s*([a-zA-Z]+)\s*$")


def unit_qty(presentation: str | None) -> tuple[str, float] | None:
    """"1 X 473 mL" -> ("ml", 473); "3 X 324 g" -> ("g", 972). Solo peso y volumen:
    las "Und" mezclan cosas incomparables (un paquete de 150 servilletas vs. una pizza)."""
    m = _PRESENTATION.match(presentation or "")
    if not m or m[3].lower() not in _UNITS:
        return None
    dim, factor = _UNITS[m[3].lower()]
    qty = int(m[1] or 1) * float(m[2].replace(",", ".")) * factor
    return (dim, qty) if qty > 0 else None


def unit_index(products) -> dict[tuple, list[float]]:
    """Precios por unidad ordenados, por (sub-pasillo, unidad), de los productos a la venta."""
    idx: dict[tuple, list[float]] = {}
    for p in products:
        q = unit_qty(p["presentation"])
        if q and p["in_stock"] and not p["global_offer"] and p["price"] > 0:
            idx.setdefault((p["subaisle"], q[0]), []).append(p["price"] / q[1])
    for v in idx.values():
        v.sort()
    return idx


def subaisle_reference(idx: dict, p: dict) -> tuple[float, int] | None:
    """Precio equivalente al percentil 10 del sub-pasillo para el tamaño de `p`, sin contar a `p`.
    Es decir: lo que costaría `p` si tuviera el precio por unidad de los más baratos de su góndola."""
    q = unit_qty(p["presentation"])
    if not q:
        return None
    prices = idx.get((p["subaisle"], q[0]), [])
    own = p["price"] / q[1]
    i = bisect.bisect_left(prices, own)
    others = prices[:i] + prices[i + 1:] if i < len(prices) and prices[i] == own else prices
    if len(others) < MIN_COMPARABLES:
        return None
    return others[len(others) // 10] * q[1], len(others)


def offer_status(rows, rules: dict, now: int) -> dict:
    """¿El precio actual es una oferta de verdad? Compara contra lo que costó antes, no contra el
    precio tachado que pone Rappi (que puede estar inflado para aparentar un descuento).

    `rows`: cambios de precio (ts, price, global_offer, in_stock) ordenados; el último es el estado
    actual. Devuelve {"status", "since", "ref_price"}:
    - "real": hace poco bajó al menos `oferta_real_baja` respecto de su precio habitual previo.
    - "inflado": cuesta lo mismo hace `oferta_permanente_dias` o más, o no es más barato que antes.
    - "sin_historial": lo vemos hace muy poco para saber.
    """
    day = 86400
    cur = rows[-1]["price"]
    i = len(rows) - 1
    while i > 0 and cur > 0 and abs(rows[i - 1]["price"] - cur) / cur <= SAME_PRICE:
        i -= 1
    since = rows[i]["ts"]
    if now - since >= rules["oferta_permanente_dias"] * day:
        return {"status": "inflado", "since": since, "ref_price": None}
    if i == 0 or since - rows[0]["ts"] < rules["oferta_min_historial_dias"] * day:
        return {"status": "sin_historial", "since": since, "ref_price": None}
    ref = db.typical_from_changes(rows[:i + 1], rules["historial_dias"], since)
    if ref is None:
        return {"status": "sin_historial", "since": since, "ref_price": None}
    status = "real" if cur <= ref * (1 - rules["oferta_real_baja"]) else "inflado"
    return {"status": status, "since": since, "ref_price": ref}


def product_url(name: str, store_id) -> str:
    # La ficha /p/{slug}-{master_product_id} es genérica: Rappi elige la tienda (ej. Coto, que puede
    # cancelar por error de precio). La web no tiene URL directa de producto dentro de una tienda,
    # así que se abre la búsqueda por nombre dentro de la tienda Turbo, donde se puede comprar.
    return f"https://www.rappi.com.ar/tiendas/{store_id}-turbo/s?term={quote(name or '')}"


def check(p: dict, typical: float | None, median: float | None, n_stores: int,
          rules: dict, include_new_user_promos: bool, aisle_ref: tuple | None = None,
          offer: dict | None = None) -> list[dict]:
    """Reglas que dispara un producto. Cada hallazgo: rule, ref_price, ratio, detail.
    `typical` = precio habitual en la tienda; `median` = mediana en otras `n_stores` tiendas;
    `aisle_ref` = (precio de referencia, n) de su sub-pasillo, ver subaisle_reference;
    `offer` = offer_status del producto."""
    price, real = p["price"], p["real_price"]

    if p["global_offer"]:
        # Promo "Máx. 1 Ud." para cuentas nuevas: verificado que no aplica a usuarios existentes.
        if include_new_user_promos and real > 0 and price / real <= rules["descuento_extremo"]:
            return [{"rule": "promo_usuario_nuevo", "ref_price": real, "ratio": price / real,
                     "detail": f"máx {p['global_offer_max']} u."}]
        return []

    # 1. Precio absurdo en valor absoluto ($0, $1...)
    if price <= rules["precio_absurdo"]:
        return [{"rule": "precio_absurdo", "ref_price": real or None,
                 "ratio": (price / real) if real else None, "detail": ""}]

    hits = []
    # 2. Caída fuerte contra el precio habitual del mismo producto en la misma tienda
    if typical and price / typical <= rules["caida_vs_historial"]:
        hits.append({"rule": "caida_vs_historial", "ref_price": typical, "ratio": price / typical,
                     "detail": f"antes ${p['prev_price']:,.0f}" if p.get("prev_price") else ""})

    # 3. Descuento extremo declarado por la propia Rappi (precio tachado)
    if real > 0 and price / real <= rules["descuento_extremo"]:
        hits.append({"rule": "descuento_extremo", "ref_price": real, "ratio": price / real, "detail": ""})
    # 3b. Oferta fuerte: no es un error, pero vale la pena enterarse. Solo si es una oferta real:
    #     un tachado alto sobre el precio de siempre no es oferta.
    elif (real > 0 and rules.get("gran_descuento") and price / real <= rules["gran_descuento"]
          and offer and offer["status"] == "real"):
        hits.append({"rule": "gran_descuento", "ref_price": real, "ratio": price / real,
                     "detail": f"antes ${offer['ref_price']:,.0f}"})

    # 4. Mucho más barato que el mismo producto en otras tiendas Turbo
    if median and price / median <= rules["vs_otras_tiendas"]:
        hits.append({"rule": "vs_otras_tiendas", "ref_price": median, "ratio": price / median,
                     "detail": f"mediana de {n_stores} tiendas"})

    # 5. Sin historial propio (producto nuevo): mucho más barato por kilo/litro que los más
    #    baratos de su sub-pasillo. Con 0.2, sobre el catálogo de oct-2026 no dispara con ningún
    #    producto vigente y detecta ~la mitad de los precios con un cero de menos.
    if typical is None and aisle_ref and rules.get("nuevo_vs_pasillo"):
        ref, n = aisle_ref
        if price / ref <= rules["nuevo_vs_pasillo"]:
            hits.append({"rule": "nuevo_vs_pasillo", "ref_price": ref, "ratio": price / ref,
                         "detail": f"vs. los más baratos por kg/L de {n} productos de su góndola"})
    return hits


def evaluate(conn, changed: list[dict], rules: dict, include_new_user_promos: bool) -> list[dict]:
    now = int(time.time())
    findings = []
    idx = None  # solo hace falta si hay productos sin historial
    for p in changed:
        if not p["in_stock"]:
            continue  # la alerta vigente (si hay) sigue abierta: puede volver con el mismo precio
        typical = median = aisle_ref = offer = None
        n = 0
        if not p["global_offer"]:
            rows = db.price_rows(conn, p["store_id"], p["product_id"], now)
            typical = db.typical_from_changes(rows, rules["historial_dias"], now)
            offer = offer_status(rows, rules, now) if rows else None
            median, n = db.other_stores_median(conn, p["master_product_id"], p["store_id"])
            if typical is None:
                if idx is None:
                    idx = unit_index(conn.execute("SELECT * FROM products WHERE store_id=?", (p["store_id"],)))
                aisle_ref = subaisle_reference(idx, p)
        base = {
            "ts": now,
            "store_id": p["store_id"],
            "product_id": p["product_id"],
            "price": p["price"],
            "name": p["name"],
            "url": product_url(p["name"], p["store_id"]),
        }
        hits = check(p, typical, median, n, rules, include_new_user_promos, aisle_ref, offer)
        db.close_alerts(conn, p["store_id"], p["product_id"], p["price"], {h["rule"] for h in hits},
                        rules["realertar_si_baja"])
        findings += [f for f in ({**base, **h} for h in hits) if db.save_alert(conn, f, rules["realertar_si_baja"])]
    conn.commit()
    return findings
