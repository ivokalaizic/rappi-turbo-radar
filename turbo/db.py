"""Persistencia en SQLite.

- products:      último estado conocido de cada producto por tienda
- price_changes: historial, una fila cada vez que cambia precio, precio de lista, promo o stock
                 (no en cada corrida); "sin stock" incluye desaparecer del catálogo
- alerts:        log de hallazgos emitidos
- alert_state:   hallazgo vigente por (tienda, producto, regla): evita repetir el aviso
                 mientras dura la misma oferta o error de precio
"""

import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS locations (
    name TEXT PRIMARY KEY, lat REAL, lng REAL, store_id INTEGER, resolved_at INTEGER
);
CREATE TABLE IF NOT EXISTS products (
    store_id INTEGER, product_id TEXT, master_product_id INTEGER,
    name TEXT, presentation TEXT, trademark TEXT, aisle TEXT, subaisle TEXT, image_url TEXT,
    price REAL, real_price REAL, in_stock INTEGER, stock INTEGER,
    global_offer INTEGER, global_offer_max INTEGER,
    first_seen INTEGER, last_seen INTEGER,
    PRIMARY KEY (store_id, product_id)
);
CREATE INDEX IF NOT EXISTS products_master ON products (master_product_id);
CREATE TABLE IF NOT EXISTS price_changes (
    store_id INTEGER, product_id TEXT, ts INTEGER,
    price REAL, real_price REAL, global_offer INTEGER, in_stock INTEGER
);
CREATE INDEX IF NOT EXISTS price_changes_product ON price_changes (store_id, product_id, ts);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY, store_id INTEGER, started INTEGER, finished INTEGER,
    products INTEGER, changed INTEGER, requests INTEGER, errors INTEGER, status TEXT
);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY, ts INTEGER, store_id INTEGER, product_id TEXT, rule TEXT,
    price REAL, ref_price REAL, ratio REAL, name TEXT, url TEXT, detail TEXT,
    notified INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS alerts_product ON alerts (store_id, product_id, rule, ts);
CREATE TABLE IF NOT EXISTS alert_state (
    store_id INTEGER, product_id TEXT, rule TEXT, price REAL, ts INTEGER,
    PRIMARY KEY (store_id, product_id, rule)
);
"""

# Hasta 2026-10 las alertas eran únicas por precio exacto: cada reajuste de ±1 % de Rappi
# volvía a avisar la misma oferta. Ahora la deduplicación vive en alert_state.
MIGRATE_ALERTS = """
BEGIN;
ALTER TABLE alerts RENAME TO alerts_old;
CREATE TABLE alerts (
    id INTEGER PRIMARY KEY, ts INTEGER, store_id INTEGER, product_id TEXT, rule TEXT,
    price REAL, ref_price REAL, ratio REAL, name TEXT, url TEXT, detail TEXT,
    notified INTEGER DEFAULT 0
);
INSERT INTO alerts SELECT * FROM alerts_old;
DROP TABLE alerts_old;
CREATE INDEX IF NOT EXISTS alerts_product ON alerts (store_id, product_id, rule, ts);
INSERT OR REPLACE INTO alert_state SELECT store_id, product_id, rule, price, ts FROM alerts ORDER BY ts;
COMMIT;
"""

TRACKED = ("price", "real_price", "global_offer", "in_stock")


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    # WAL: la UI puede leer mientras el scraper escribe
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(SCHEMA)
    if "UNIQUE" in conn.execute("SELECT sql FROM sqlite_master WHERE name='alerts'").fetchone()[0]:
        conn.executescript(MIGRATE_ALERTS)
    if "covered" not in {r[1] for r in conn.execute("PRAGMA table_info(locations)")}:
        conn.execute("ALTER TABLE locations ADD COLUMN covered INTEGER DEFAULT 1")
    return conn


def normalize(store_id: int, aisle: str, subaisle: str, p: dict) -> dict:
    return {
        "store_id": store_id,
        "product_id": str(p["product_id"]),
        "master_product_id": p.get("master_product_id"),
        "name": p.get("name"),
        "presentation": p.get("presentation"),
        "trademark": p.get("trademark"),
        "aisle": aisle,
        "subaisle": subaisle,
        "image_url": p.get("image_url"),
        "price": float(p.get("price") or 0),
        "real_price": float(p.get("real_price") or 0),
        "in_stock": int(bool(p.get("in_stock"))),
        "stock": p.get("stock"),
        # Oferta "Máx. 1 Ud.": promo para usuarios nuevos, verificado que no aplica a cuentas
        # existentes. Las de tope 6/10 o sin tope son descuentos comunes.
        "global_offer": int(bool(p.get("has_global_offers")) and p.get("global_offer_max_quantity") == 1),
        "global_offer_max": p.get("global_offer_max_quantity"),
    }


def upsert_products(conn, rows: list[dict], now: int) -> list[dict]:
    """Guarda el estado actual. Devuelve los productos nuevos o con cambios,
    cada uno con `prev_price` (None si es nuevo)."""
    changed = []
    for r in rows:
        old = conn.execute(
            "SELECT price, real_price, global_offer, in_stock FROM products WHERE store_id=? AND product_id=?",
            (r["store_id"], r["product_id"]),
        ).fetchone()
        if old is None or any(old[k] != r[k] for k in TRACKED):
            conn.execute(
                "INSERT INTO price_changes VALUES (?,?,?,?,?,?,?)",
                (r["store_id"], r["product_id"], now, r["price"], r["real_price"], r["global_offer"], r["in_stock"]),
            )
            changed.append({**r, "prev_price": old["price"] if old else None})
        conn.execute(
            """INSERT INTO products VALUES (:store_id,:product_id,:master_product_id,:name,:presentation,
                   :trademark,:aisle,:subaisle,:image_url,:price,:real_price,:in_stock,:stock,
                   :global_offer,:global_offer_max,:now,:now)
               ON CONFLICT (store_id, product_id) DO UPDATE SET
                   master_product_id=excluded.master_product_id, name=excluded.name,
                   presentation=excluded.presentation, trademark=excluded.trademark,
                   aisle=excluded.aisle, subaisle=excluded.subaisle, image_url=excluded.image_url,
                   price=excluded.price, real_price=excluded.real_price, in_stock=excluded.in_stock,
                   stock=excluded.stock, global_offer=excluded.global_offer,
                   global_offer_max=excluded.global_offer_max, last_seen=excluded.last_seen""",
            {**r, "now": now},
        )
    conn.commit()
    return changed


def mark_missing(conn, store_id: int, now: int, failed: set) -> int:
    """Productos que no vinieron en una corrida completa: Rappi saca del catálogo lo que se queda
    sin stock, así que se marcan sin stock (y queda en el historial). Si vuelven, el cambio
    in_stock 0→1 hace que se evalúen de nuevo. `failed`: pasillos y (pasillo, sub-pasillo) que
    fallaron en esta corrida; sus productos no se tocan porque no sabemos si siguen."""
    gone = [r for r in conn.execute(
        "SELECT * FROM products WHERE store_id=? AND last_seen<? AND in_stock=1", (store_id, now))
        if r["aisle"] not in failed and (r["aisle"], r["subaisle"]) not in failed]
    for r in gone:
        conn.execute("INSERT INTO price_changes VALUES (?,?,?,?,?,?,?)",
                     (store_id, r["product_id"], now, r["price"], r["real_price"], r["global_offer"], 0))
        conn.execute("UPDATE products SET in_stock=0 WHERE store_id=? AND product_id=?", (store_id, r["product_id"]))
    conn.commit()
    return len(gone)


def typical_price(conn, store_id: int, product_id: str, days: int, now: int | None = None) -> float | None:
    """Precio que el producto mantuvo más tiempo en los últimos `days` días,
    sin contar el tramo actual. None si no hay historial previo."""
    now = now or int(time.time())
    rows = conn.execute(
        """SELECT ts, price, global_offer, in_stock FROM price_changes
           WHERE store_id=? AND product_id=? AND ts <= ? ORDER BY ts""",
        (store_id, product_id, now),
    ).fetchall()
    return typical_from_changes(rows, days, now)


def typical_from_changes(rows, days: int, now: int) -> float | None:
    """`rows`: todos los cambios (ts, price, global_offer, in_stock) ordenados; el último es el
    estado actual y no cuenta. Tampoco cuentan los tramos de promo para usuarios nuevos ni los
    sin stock: ese precio no estaba realmente a la venta."""
    since = now - days * 86400
    held: dict[float, float] = {}
    for r, nxt in zip(rows, rows[1:]):
        if r["global_offer"] or not r["in_stock"]:
            continue
        start = max(r["ts"], since)
        if nxt["ts"] > start:
            held[r["price"]] = held.get(r["price"], 0) + (nxt["ts"] - start)
    return max(held, key=held.get) if held else None


def other_stores_median(conn, master_product_id, store_id) -> tuple[float | None, int]:
    prices = sorted(
        r["price"]
        for r in conn.execute(
            """SELECT price FROM products
               WHERE master_product_id=? AND store_id<>? AND in_stock=1 AND global_offer=0 AND price>0""",
            (master_product_id, store_id),
        )
    )
    if not prices:
        return None, 0
    mid = len(prices) // 2
    median = prices[mid] if len(prices) % 2 else (prices[mid - 1] + prices[mid]) / 2
    return median, len(prices)


def save_alert(conn, a: dict, realert_drop: float) -> bool:
    """Guarda la alerta si es un hallazgo nuevo. Si esa regla ya está vigente para el producto,
    solo se vuelve a avisar cuando el precio baja más de `realert_drop` (0.05 = 5 %) respecto
    del último aviso. True si se guardó."""
    prev = conn.execute("SELECT price FROM alert_state WHERE store_id=? AND product_id=? AND rule=?",
                        (a["store_id"], a["product_id"], a["rule"])).fetchone()
    if prev and a["price"] >= prev["price"] * (1 - realert_drop):
        return False
    conn.execute(
        """INSERT INTO alerts (ts, store_id, product_id, rule, price, ref_price, ratio, name, url, detail)
           VALUES (:ts,:store_id,:product_id,:rule,:price,:ref_price,:ratio,:name,:url,:detail)""",
        a,
    )
    conn.execute("INSERT OR REPLACE INTO alert_state VALUES (?,?,?,?,?)",
                 (a["store_id"], a["product_id"], a["rule"], a["price"], a["ts"]))
    conn.commit()
    return True


def close_alerts(conn, store_id: int, product_id: str, price: float, keep: set[str], margin: float):
    """El producto se evaluó y ya no dispara estas reglas: la oferta o el error terminó, y si vuelve
    a pasar más adelante se avisa de nuevo. Para no cerrar y reabrir cuando el precio baila alrededor
    del umbral (p. ej. 0,502 vs 0,5), solo se cierra si además subió más de `margin` sobre el aviso."""
    conn.execute(f"""DELETE FROM alert_state WHERE store_id=? AND product_id=? AND price * ? < ?
                     AND rule NOT IN ({",".join("?" * len(keep))})""",
                 (store_id, product_id, 1 + margin, price, *keep))
