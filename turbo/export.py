"""Lo que muestra la UI: productos de una tienda con sus reglas, historial y alertas.

Lo usan la UI local (ui.py) y `python -m turbo export`, que deja el mismo resumen en archivos
para la UI en la nube. El export no lleva nombres ni coordenadas de las direcciones.
"""

import gzip
import json
import time
from pathlib import Path

from . import db, detect

UI_RULES = {"precio_absurdo": 10, "caida_vs_historial": 0.5, "descuento_extremo": 0.2,
            "vs_otras_tiendas": 0.5, "gran_descuento": 0.5, "historial_dias": 14}


def rules_of(cfg: dict) -> dict:
    return {**UI_RULES, **cfg.get("rules", {})}


def products_payload(conn, store_id: int, cfg: dict, with_history: bool = False) -> dict:
    rules = rules_of(cfg)
    include_promos = cfg.get("include_new_user_promos", False)
    now = int(time.time())

    changes: dict[str, list] = {}
    for r in conn.execute("SELECT product_id, ts, price, real_price, global_offer, in_stock FROM price_changes "
                          "WHERE store_id=? ORDER BY ts", (store_id,)):
        changes.setdefault(r["product_id"], []).append(r)

    others: dict[int, list[float]] = {}
    for r in conn.execute("SELECT master_product_id, price FROM products WHERE store_id<>? AND in_stock=1 "
                          "AND global_offer=0 AND price>0", (store_id,)):
        others.setdefault(r["master_product_id"], []).append(r["price"])

    alerts: dict[str, list] = {}
    for r in conn.execute("SELECT product_id, ts, rule, price, ref_price, ratio, detail FROM alerts "
                          "WHERE store_id=? ORDER BY ts DESC", (store_id,)):
        alerts.setdefault(r["product_id"], []).append({k: r[k] for k in r.keys() if k != "product_id"})

    rows = [dict(r) for r in conn.execute("SELECT * FROM products WHERE store_id=?", (store_id,))]
    idx = detect.unit_index(rows)
    items = []
    for p in rows:
        hist = changes.get(p["product_id"], [])
        clean = [h for h in hist if not h["global_offer"]]
        prices = [h["price"] for h in clean] or [p["price"]]
        p["prev_price"] = hist[-2]["price"] if len(hist) >= 2 else None
        typical = db.typical_from_changes(hist, rules["historial_dias"], now)
        aisle_ref = detect.subaisle_reference(idx, p) if typical is None else None
        o = sorted(others.get(p["master_product_id"], []))
        median = (o[len(o) // 2] if len(o) % 2 else (o[len(o) // 2 - 1] + o[len(o) // 2]) / 2) if o else None
        p.update(
            typical_price=typical, other_median=median, other_stores=len(o),
            min_price=min(prices), max_price=max(prices), n_changes=len(hist),
            last_change=hist[-1]["ts"] if hist else None,
            discount=(1 - p["price"] / p["real_price"]) if p["real_price"] > p["price"] else 0,
            flags=detect.check(p, typical, median, len(o), rules, include_promos, aisle_ref),
            alerts=alerts.get(p["product_id"], []),
            url=detect.product_url(p["name"], store_id),
        )
        if with_history:  # la UI en la nube no puede pedir el historial producto por producto
            p["history"] = [dict(h) for h in hist]
        items.append(p)

    run = conn.execute("SELECT * FROM runs WHERE store_id=? ORDER BY id DESC LIMIT 1", (store_id,)).fetchone()
    loc_rows = conn.execute("SELECT name, covered FROM locations WHERE store_id=?", (store_id,)).fetchall()
    return {"store_id": store_id, "locations": [r["name"] for r in loc_rows], "run": dict(run) if run else None,
            "covered": any(r["covered"] for r in loc_rows) if loc_rows else True,
            "rules": rules, "include_new_user_promos": include_promos, "products": items}


def history_payload(conn, store_id: int, product_id: str) -> list[dict]:
    rows = conn.execute("SELECT ts, price, real_price, global_offer, in_stock FROM price_changes "
                        "WHERE store_id=? AND product_id=? ORDER BY ts", (store_id, product_id)).fetchall()
    return [dict(r) for r in rows]


def export(conn, cfg: dict, out: Path) -> list[Path]:
    """Un `store-<id>.json.gz` por tienda de las direcciones configuradas + `index.json`."""
    out.mkdir(parents=True, exist_ok=True)
    names = [l["name"] for l in cfg.get("locations", [])]
    store_ids = sorted({r["store_id"] for r in conn.execute(
        f"SELECT store_id FROM locations WHERE store_id IS NOT NULL AND name IN ({','.join('?' * len(names))})",
        names)})
    files, stores = [], []
    for i, store_id in enumerate(store_ids, 1):
        label = f"Dirección {i}" if len(store_ids) > 1 else "Mi dirección"
        payload = products_payload(conn, store_id, cfg, with_history=True)
        payload["locations"] = [label]  # los nombres reales de las direcciones no salen de GitHub
        path = out / f"store-{store_id}.json.gz"
        path.write_bytes(gzip.compress(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()))
        files.append(path)
        stores.append({"store_id": store_id, "label": label,
                       "covered": payload["covered"], "run": payload["run"]})
    index = out / "index.json"
    index.write_text(json.dumps({"exported": int(time.time()), "stores": stores}, ensure_ascii=False))
    return [index, *files]
