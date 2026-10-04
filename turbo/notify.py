"""Avisos proactivos de alertas nuevas (las que todavía tienen notified=0).

- macOS: un cartel con los hallazgos y un botón que abre la UI en esa tienda.
- Telegram: si `notify.telegram.bot_token` y `chat_id` están cargados en config.json.
"""

import json
import logging
import subprocess
import sys
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)

TITLES = {
    "precio_absurdo": "Precio absurdo",
    "caida_vs_historial": "Bajó vs. su precio habitual",
    "descuento_extremo": "Descuento extremo",
    "vs_otras_tiendas": "Más barato que en otras tiendas",
    "promo_usuario_nuevo": "Promo usuario nuevo",
    "gran_descuento": "Oferta real fuerte",
    "nuevo_vs_pasillo": "Producto nuevo muy barato para su góndola",
}
MAX_LINES = 6


def pending(conn) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM alerts WHERE notified=0 ORDER BY ratio")]


def mark_notified(conn, alerts: list[dict]):
    conn.executemany("UPDATE alerts SET notified=1 WHERE id=?", [(a["id"],) for a in alerts])
    conn.commit()


def money(v) -> str:
    return "$" + f"{v:,.2f}".rstrip("0").rstrip(".").replace(",", "X").replace(".", ",").replace("X", ".")


def line(a: dict) -> str:
    off = f" (−{int((1 - a['ratio']) * 100)} %)" if a.get("ratio") is not None else ""
    ref = f" vs {money(a['ref_price'])}" if a.get("ref_price") else ""
    return f"{a['name']}: {money(a['price'])}{ref}{off}"


def send(conn, cfg: dict):
    alerts = pending(conn)
    if not alerts:
        return
    ncfg = cfg.get("notify", {})
    # Un producto puede disparar varias reglas: se avisa una vez, con la más fuerte
    by_product = {}
    for a in alerts:
        by_product.setdefault((a["store_id"], a["product_id"]), a)
    items = list(by_product.values())
    weird = [a for a in items if a["rule"] != "gran_descuento"]
    deals = [a for a in items if a["rule"] == "gran_descuento"]
    head = " · ".join(filter(None, [
        f"{len(weird)} precio(s) raro(s)" if weird else "",
        f"{len(deals)} oferta(s) fuerte(s)" if deals else "",
    ]))
    url = f"{ncfg.get('ui_url', 'http://127.0.0.1:8765').rstrip('/')}/?store={items[0]['store_id']}&view=alerts"

    if ncfg.get("macos", True) and sys.platform == "darwin":
        _macos(head, [line(a) for a in (weird + deals)[:MAX_LINES]], len(items), url)
    tg = ncfg.get("telegram") or {}
    if tg.get("bot_token") and tg.get("chat_id"):
        _telegram(tg, head, weird + deals)
    mark_notified(conn, alerts)


def test(cfg: dict) -> list[str]:
    """Manda un mensaje de prueba por cada canal configurado; devuelve los canales que funcionaron."""
    ncfg, ok = cfg.get("notify", {}), []
    tg = ncfg.get("telegram") or {}
    text = "✅ Turbo Radar: prueba de aviso. Si ves esto, las alertas te van a llegar por acá."
    if tg.get("bot_token") and tg.get("chat_id"):
        try:
            _tg_call(tg, "sendMessage", chat_id=tg["chat_id"], text=text)
            ok.append("telegram")
        except Exception as e:
            log.error("Telegram falló: %s", e)
    return ok


def _as(text: str) -> str:
    """String literal de AppleScript (entiende \\n y \\", no \\uXXXX)."""
    return json.dumps(text, ensure_ascii=False)


def _macos(head, lines, total, url):
    body = "\n".join("• " + l for l in lines)
    if total > len(lines):
        body += f"\n… y {total - len(lines)} más"
    script = (f"activate\nset r to display dialog {_as(body)} with title {_as('Turbo Radar — ' + head)} "
              f'buttons {{"Cerrar", "Ver ofertas"}} default button "Ver ofertas" giving up after 3600\n'
              f'if button returned of r is "Ver ofertas" then open location {_as(url)}')
    # Sin esperar: la corrida no se queda colgada hasta que alguien toque el cartel.
    subprocess.Popen(["osascript", "-e", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    subprocess.run(["osascript", "-e", f"display notification {_as(lines[0])} with title \"Turbo Radar\" "
                    f"subtitle {_as(head)} sound name \"Glass\""], capture_output=True)


def _tg_call(tg, method, **params):
    req = urllib.request.Request(f"https://api.telegram.org/bot{tg['bot_token']}/{method}",
                                 data=urllib.parse.urlencode(params).encode())
    urllib.request.urlopen(req, timeout=20).read()


def _item_text(a: dict) -> str:
    return f"{'🚨' if a['rule'] != 'gran_descuento' else '🔥'} {TITLES.get(a['rule'], a['rule'])}\n{line(a)}\n{a['url']}"


def _telegram(tg, head, items):
    try:
        _tg_call(tg, "sendMessage", chat_id=tg["chat_id"], text=f"🛒 Turbo Radar — {head}")
        for a in items[:10]:
            _tg_call(tg, "sendMessage", chat_id=tg["chat_id"], text=_item_text(a))
    except Exception as e:  # un aviso fallido no debe romper la corrida
        log.error("Telegram falló: %s", e)

