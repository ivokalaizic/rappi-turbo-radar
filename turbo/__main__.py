"""CLI: python -m turbo {run,stores,alerts}"""

import argparse
import fcntl
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from . import api, db, detect, export, notify

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DEFAULT_CONFIG = ROOT / "config.json"

COVERAGE_RETRIES_S = (20, 40)  # antes de dar una dirección por sin cobertura
SCHEDULE = DATA / "schedule.json"  # cuándo arrancó la última corrida programada (la UI muestra la próxima)

log = logging.getLogger("turbo")


def load_config(path: Path) -> dict:
    if not path.exists():
        sys.exit(f"No existe {path}. Copiá config.example.json a config.json y cargá tus direcciones.")
    cfg = json.loads(path.read_text())
    # Reglas nuevas toman su default aunque el config (o el secret CONFIG_JSON) no las tenga
    cfg["rules"] = {**detect.DEFAULT_RULES, **cfg.get("rules", {})}
    return cfg


def resolve_stores(conn, client, locations) -> dict[int, dict]:
    """Agrupa las direcciones por tienda: varias direcciones pueden caer en la misma.

    Se resuelve en cada corrida (1 request): la cobertura de Turbo cambia durante el día y una
    alerta solo sirve si esa tienda te entrega ahora. Sin cobertura no se scrapea ni se alerta,
    pero se conserva la última tienda conocida para no perder el historial en la UI.
    """
    stores: dict[int, dict] = {}
    now = int(time.time())
    for loc in locations:
        row = conn.execute("SELECT * FROM locations WHERE name=?", (loc["name"],)).fetchone()
        store = client.turbo_store_for(loc["lat"], loc["lng"])
        for wait in COVERAGE_RETRIES_S:  # la tienda a veces desaparece unos minutos del router
            if store:
                break
            log.info("Turbo no aparece para %s, reintento en %ds", loc["name"], wait)
            time.sleep(wait)
            store = client.turbo_store_for(loc["lat"], loc["lng"])
        store_id = store["store_id"] if store else None
        same_place = row and row["lat"] == loc["lat"] and row["lng"] == loc["lng"]
        conn.execute("INSERT OR REPLACE INTO locations (name, lat, lng, store_id, resolved_at, covered) "
                     "VALUES (?,?,?,?,?,?)",
                     (loc["name"], loc["lat"], loc["lng"],
                      store_id or (row["store_id"] if same_place else None), now, int(store_id is not None)))
        conn.commit()
        if store_id is None:
            log.warning("sin cobertura Turbo ahora para %s, salteo", loc["name"])
            continue
        stores.setdefault(store_id, {"lat": loc["lat"], "lng": loc["lng"], "locations": []})["locations"].append(loc["name"])
    return stores


def print_findings(findings, store_names):
    for f in findings:
        ref = f"${f['ref_price']:,.0f}" if f.get("ref_price") else "-"
        pct = f" ({(1 - f['ratio']) * 100:.0f}% menos)" if f.get("ratio") is not None else ""
        print(f"🚨 [{f['rule']}] {f['name']}\n"
              f"   ${f['price']:,.2f} vs ref {ref}{pct} {f.get('detail') or ''}\n"
              f"   Tienda {f['store_id']} ({', '.join(store_names.get(f['store_id'], []))})\n"
              f"   {f['url']}")


def scrape_store(conn, client, store_id, s, cfg, on_product=None) -> list[dict]:
    """Scrapea una tienda, guarda el estado y devuelve las alertas nuevas.
    `on_product(aisle, n_productos)` sirve para mostrar progreso (lo usa la UI)."""
    started = int(time.time())
    req0, err0 = client.requests, client.errors
    rows, status = [], "ok"
    try:
        for aisle, sub, p in client.crawl(store_id, s["lat"], s["lng"]):
            rows.append(db.normalize(store_id, aisle, sub, p))
            if on_product:
                on_product(aisle, len(rows))
    except api.RappiError as e:
        status = f"error: {e}"
        log.error("tienda %s: %s", store_id, e)

    # Una corrida muy incompleta no debe pisar el estado (evita falsos "nuevos" y "desaparecidos").
    # Se compara con lo que vino en la última corrida guardada, no con todo lo visto alguna vez.
    known = conn.execute("SELECT COUNT(*) FROM products WHERE store_id=? AND last_seen="
                         "(SELECT MAX(last_seen) FROM products WHERE store_id=?)", (store_id, store_id)).fetchone()[0]
    now, gone = int(time.time()), 0
    if known and len(rows) < known * 0.5:
        status = f"incompleta: {len(rows)} de ~{known}"
        log.error("tienda %s %s, no guardo", store_id, status)
        changed = []
    else:
        changed = db.upsert_products(conn, rows, now)
        if status == "ok":
            gone = db.mark_missing(conn, store_id, now, client.failed)

    findings = detect.evaluate(conn, changed, cfg["rules"], cfg.get("include_new_user_promos", False))
    conn.execute("INSERT INTO runs (store_id, started, finished, products, changed, requests, errors, status) "
                 "VALUES (?,?,?,?,?,?,?,?)",
                 (store_id, started, int(time.time()), len(rows), len(changed),
                  client.requests - req0, client.errors - err0, status))
    conn.commit()
    log.info("tienda %s (%s): %d productos, %d cambios, %d desaparecidos, %d alertas nuevas, %d requests, %ds — %s",
             store_id, ", ".join(s["locations"]), len(rows), len(changed), gone, len(findings),
             client.requests - req0, int(time.time()) - started, status)
    return findings


def write_schedule(**kw):
    """Solo para corridas de launchd: TURBO_INTERVAL_S viene del plist."""
    interval = os.environ.get("TURBO_INTERVAL_S")
    if not interval:
        return
    try:
        state = json.loads(SCHEDULE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    SCHEDULE.write_text(json.dumps({**state, "interval": int(interval), **kw}))


def cmd_run(args, cfg):
    DATA.mkdir(exist_ok=True)
    # launchd cuenta el intervalo desde que dispara, aunque la corrida se saltee por el lock
    write_schedule(last_start=int(time.time()), running=False)
    lock = open(DATA / "run.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.warning("ya hay una corrida en curso, salteo esta")
        return
    write_schedule(running=True)
    try:
        _run(cfg, baseline=args.baseline)
    finally:
        write_schedule(running=False, last_end=int(time.time()))


def _run(cfg, baseline=False):
    conn = db.connect(DATA / "turbo.db")
    client = api.Client(DATA, cfg.get("app_version", "web_v1.223.2"), cfg.get("request_delay_s", 0.3))
    stores = resolve_stores(conn, client, cfg["locations"])
    all_findings = []

    for store_id, s in stores.items():
        all_findings += scrape_store(conn, client, store_id, s, cfg)

    print_findings(all_findings, {k: v["locations"] for k, v in stores.items()})
    if baseline:  # base nueva: todo es "nuevo", no tiene sentido avisar de cada oferta vigente
        notify.mark_notified(conn, notify.pending(conn))
    else:
        notify.send(conn, cfg)
    if not all_findings:
        print("Sin hallazgos nuevos.")


def cmd_stores(args, cfg):
    conn = db.connect(DATA / "turbo.db")
    client = api.Client(DATA, cfg.get("app_version", "web_v1.223.2"))
    for store_id, s in resolve_stores(conn, client, cfg["locations"]).items():
        n = conn.execute("SELECT COUNT(*) FROM products WHERE store_id=?", (store_id,)).fetchone()[0]
        print(f"Tienda {store_id}: {', '.join(s['locations'])} — {n} productos en la base")


def cmd_alerts(args, cfg):
    conn = db.connect(DATA / "turbo.db")
    since = int(time.time()) - args.hours * 3600
    rows = conn.execute("SELECT * FROM alerts WHERE ts>=? ORDER BY ts DESC", (since,)).fetchall()
    for r in rows:
        when = datetime.fromtimestamp(r["ts"]).strftime("%d/%m %H:%M")
        ref = f"${r['ref_price']:,.0f}" if r["ref_price"] else "-"
        print(f"{when}  [{r['rule']}] ${r['price']:,.2f} (ref {ref})  {r['name']}\n    {r['url']}")
    if not rows:
        print(f"Sin alertas en las últimas {args.hours} h.")


def cmd_export(args, cfg):
    conn = db.connect(DATA / "turbo.db")
    for path in export.export(conn, cfg, args.out):
        print(f"{path} ({path.stat().st_size // 1024} KB)")


def cmd_notify_test(args, cfg):
    ok = notify.test(cfg)
    if not ok:
        sys.exit("Ningún canal funcionó (o no hay ninguno configurado en notify).")
    print("Prueba enviada por: " + ", ".join(ok))


def main():
    parser = argparse.ArgumentParser(prog="turbo", description="Detector de precios mal puestos en Rappi Turbo")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_run = sub.add_parser("run", help="scrapea todas las tiendas y detecta precios raros")
    p_run.add_argument("--baseline", action="store_true",
                       help="guarda el catálogo y las alertas sin avisar (primera corrida con base vacía)")
    sub.add_parser("stores", help="muestra qué tienda Turbo atiende cada dirección")
    sub.add_parser("notify-test", help="manda un mensaje de prueba por Telegram")
    p_export = sub.add_parser("export", help="deja el resumen de cada tienda en archivos para la UI en la nube")
    p_export.add_argument("--out", type=Path, default=DATA / "export")
    p_alerts = sub.add_parser("alerts", help="lista las alertas recientes")
    p_alerts.add_argument("--hours", type=int, default=24)
    args = parser.parse_args()

    DATA.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(DATA / "turbo.log")],
    )
    cfg = load_config(args.config)
    {"run": cmd_run, "stores": cmd_stores, "alerts": cmd_alerts, "notify-test": cmd_notify_test,
     "export": cmd_export}[args.cmd](args, cfg)


if __name__ == "__main__":
    main()
