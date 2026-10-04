"""Cliente mínimo de la API web de Rappi Argentina, usada como invitado (sin cuenta).

Flujo:
  1. GET  /api/rocket/v2/guest/passport/            -> token "pasaporte"
  2. POST /api/rocket/v2/guest (x-guest-api-key)    -> access_token (dura 7 días)
  3. GET  stores-router/available/principal/?lat&lng -> tienda Turbo para esa ubicación
  4. POST dynamic/context/content/                  -> pasillos, sub-pasillos y productos
"""

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

log = logging.getLogger(__name__)

BASE = "https://services.rappi.com.ar"
CONTENT = "/api/web-gateway/web/dynamic/context/content/"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"
)
TOKEN_MAX_AGE_S = 6 * 24 * 3600  # el token dura 7 días; lo renovamos antes
RETRYABLE = {429, 500, 502, 503, 504}


class RappiError(Exception):
    pass


class Client:
    def __init__(self, state_dir: Path, app_version: str, delay_s: float = 0.3):
        self.session_path = state_dir / "session.json"
        self.app_version = app_version
        self.delay_s = delay_s
        self.requests = 0
        self.errors = 0
        try:
            self.session = json.loads(self.session_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            self.session = {}
        self.session.setdefault("deviceid", str(uuid.uuid4()))

    # --- HTTP -------------------------------------------------------------

    def _headers(self, auth: bool) -> dict:
        h = {
            "User-Agent": USER_AGENT,
            "Origin": "https://www.rappi.com.ar",
            "Referer": "https://www.rappi.com.ar/",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "accept-language": "es-AR",
            "language": "es",
            "deviceid": self.session["deviceid"],
            "app-version": self.app_version,
            "needAppsFlyerId": "false",
            "include_context_info": "true",
        }
        if auth:
            h["Authorization"] = "Bearer " + self._token()
        return h

    def _request(self, method, path, body=None, extra_headers=None, auth=True, retries=4):
        for attempt in range(retries):
            headers = {**self._headers(auth), **(extra_headers or {})}
            data = json.dumps(body).encode() if body is not None else None
            req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
            self.requests += 1
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    raw = resp.read()
                    # 204 / cuerpo vacío = no hay más resultados
                    return json.loads(raw) if raw else None
            except urllib.error.HTTPError as e:
                self.errors += 1
                if e.code == 401 and auth and attempt == 0:
                    log.info("token rechazado, pidiendo uno nuevo")
                    self._new_token()
                    continue
                if e.code not in RETRYABLE or attempt == retries - 1:
                    raise RappiError(f"{method} {path} -> HTTP {e.code}") from e
            except (urllib.error.URLError, TimeoutError) as e:
                self.errors += 1
                if attempt == retries - 1:
                    raise RappiError(f"{method} {path} -> {e}") from e
            wait = 1.5 * 2**attempt
            log.debug("reintento %s %s en %.1fs", method, path, wait)
            time.sleep(wait)

    # --- Auth -------------------------------------------------------------

    def _token(self) -> str:
        age = time.time() - self.session.get("obtained_at", 0)
        if not self.session.get("access_token") or age > TOKEN_MAX_AGE_S:
            self._new_token()
        return self.session["access_token"]

    def _new_token(self):
        passport = self._request("GET", "/api/rocket/v2/guest/passport/", auth=False)["token"]
        tok = self._request("POST", "/api/rocket/v2/guest", {}, {"x-guest-api-key": passport}, auth=False)
        self.session.update(access_token=tok["access_token"], obtained_at=time.time())
        self.session_path.parent.mkdir(parents=True, exist_ok=True)
        self.session_path.write_text(json.dumps(self.session))
        log.info("nuevo token de invitado obtenido")

    # --- Tiendas y catálogo -----------------------------------------------

    def turbo_store_for(self, lat: float, lng: float) -> dict | None:
        """Devuelve la tienda Turbo principal que atiende esa ubicación."""
        q = urllib.parse.urlencode({"lat": lat, "lng": lng})
        data = self._request("GET", f"/api/web-gateway/web/stores-router/available/principal/?{q}")
        # La tienda aparece anidada (vertical -> suboptions -> stores); las otras turbo_* son sub-verticales
        stack = [data]
        while stack:
            o = stack.pop()
            if isinstance(o, list):
                stack.extend(o)
            elif isinstance(o, dict):
                if o.get("store_type") == "turbo" and o.get("store_id"):
                    return {"store_id": int(o["store_id"]), "lat": o.get("lat"), "lng": o.get("lng")}
                stack.extend(o.values())
        return None

    def _content(self, store_id, lat, lng, context, state=None, limit=50, offset=0):
        st = {"lat": str(lat), "lng": str(lng), "store_type": "turbo", "parent_store_type": "turbo_home", **(state or {})}
        body = {"limit": limit, "offset": offset, "state": st, "stores": [store_id], "context": context}
        time.sleep(self.delay_s)
        return self._request("POST", CONTENT, body)

    def aisles(self, store_id, lat, lng) -> list[dict]:
        data = self._content(store_id, lat, lng, "aisles_tree", limit=100)
        for comp in data["data"]["components"]:
            if comp.get("name") == "aisles_icons_carousel":
                return comp["resource"]["aisle_icons"]
        raise RappiError("no encontré la lista de pasillos (¿cambió la API?)")

    def subaisles(self, store_id, lat, lng, aisle_id) -> list[dict]:
        state = {"aisle_id": str(aisle_id), "parent_id": str(aisle_id)}
        data = self._content(store_id, lat, lng, "sub_aisles", state)
        comps = (data or {}).get("data", {}).get("components", [])
        return [c["resource"] for c in comps if c.get("name") == "aisles" and c["resource"].get("id")]

    def subaisle_products(self, store_id, lat, lng, aisle_id, subaisle_id, expected=0) -> dict[str, dict]:
        state = {"aisle_id": str(subaisle_id), "parent_id": str(aisle_id)}
        found, offset = {}, 0
        while True:
            data = self._content(store_id, lat, lng, "aisle_detail", state, offset=offset)
            comps = (data or {}).get("data", {}).get("components", [])
            before = len(found)
            _collect_products(comps, found)
            # normalmente el sub-pasillo entero viene en la primera página
            if not comps or len(found) == before or (expected and len(found) >= expected):
                return found
            offset += 50

    def crawl(self, store_id, lat, lng):
        """Recorre la tienda entera. Genera (pasillo, sub-pasillo, producto)."""
        seen = set()
        for aisle in self.aisles(store_id, lat, lng):
            try:
                subs = self.subaisles(store_id, lat, lng, aisle["id"])
            except RappiError as e:
                log.warning("pasillo %s falló: %s", aisle.get("name"), e)
                continue
            for sub in subs:
                try:
                    products = self.subaisle_products(store_id, lat, lng, aisle["id"], sub["id"], sub.get("product_count") or 0)
                except RappiError as e:
                    log.warning("sub-pasillo %s/%s falló: %s", aisle.get("name"), sub.get("name"), e)
                    continue
                for pid, p in products.items():
                    if pid not in seen:
                        seen.add(pid)
                        yield aisle["name"], sub.get("name"), p


def _collect_products(obj, acc):
    if isinstance(obj, list):
        for x in obj:
            _collect_products(x, acc)
    elif isinstance(obj, dict):
        if "product_id" in obj and "price" in obj:
            acc[str(obj["product_id"])] = obj
        else:
            for v in obj.values():
                _collect_products(v, acc)
