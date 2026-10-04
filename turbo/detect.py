"""Reglas para detectar precios sospechosamente bajos.

Solo se evalúan productos nuevos o cuyo precio cambió en esta corrida, así un
mismo hallazgo no se repite cada 15 minutos.
"""

import time
from urllib.parse import quote

from . import db


def product_url(name: str, store_id) -> str:
    # La ficha /p/{slug}-{master_product_id} es genérica: Rappi elige la tienda (ej. Coto, que puede
    # cancelar por error de precio). La web no tiene URL directa de producto dentro de una tienda,
    # así que se abre la búsqueda por nombre dentro de la tienda Turbo, donde se puede comprar.
    return f"https://www.rappi.com.ar/tiendas/{store_id}-turbo/s?term={quote(name or '')}"


def check(p: dict, typical: float | None, median: float | None, n_stores: int,
          rules: dict, include_new_user_promos: bool) -> list[dict]:
    """Reglas que dispara un producto. Cada hallazgo: rule, ref_price, ratio, detail.
    `typical` = precio habitual en la tienda; `median` = mediana en otras `n_stores` tiendas."""
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
    # 3b. Oferta fuerte: no es un error, pero vale la pena enterarse
    elif real > 0 and rules.get("gran_descuento") and price / real <= rules["gran_descuento"]:
        hits.append({"rule": "gran_descuento", "ref_price": real, "ratio": price / real, "detail": ""})

    # 4. Mucho más barato que el mismo producto en otras tiendas Turbo
    if median and price / median <= rules["vs_otras_tiendas"]:
        hits.append({"rule": "vs_otras_tiendas", "ref_price": median, "ratio": price / median,
                     "detail": f"mediana de {n_stores} tiendas"})
    return hits


def evaluate(conn, changed: list[dict], rules: dict, include_new_user_promos: bool) -> list[dict]:
    now = int(time.time())
    findings = []
    for p in changed:
        if not p["in_stock"]:
            continue
        typical = median = None
        n = 0
        if not p["global_offer"]:
            typical = db.typical_price(conn, p["store_id"], p["product_id"], rules["historial_dias"], now)
            median, n = db.other_stores_median(conn, p["master_product_id"], p["store_id"])
        base = {
            "ts": now,
            "store_id": p["store_id"],
            "product_id": p["product_id"],
            "price": p["price"],
            "name": p["name"],
            "url": product_url(p["name"], p["store_id"]),
        }
        findings += [{**base, **h} for h in check(p, typical, median, n, rules, include_new_user_promos)]

    return [f for f in findings if db.save_alert(conn, f)]
