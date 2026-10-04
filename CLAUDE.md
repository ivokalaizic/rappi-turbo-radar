# Rappi Turbo Radar

Detector de precios mal puestos y ofertas en Rappi Turbo (Buenos Aires). El dueño está
aprendiendo desarrollo: explicá los pasos de git/deploy en castellano y sin jerga innecesaria.

## Cómo está armado

- `turbo/`: el motor. `api.py` (API web de Rappi como invitado), `db.py` (SQLite), `detect.py`
  (reglas), `notify.py` (Telegram), `export.py` (resumen para la UI), `__main__.py` (CLI).
- `ui.py`: UI local (http.server, sin dependencias). Su HTML (`PAGE`) es la única fuente de la página.
- `web/`: la misma UI en Vercel (https://rappi-turbo-radar.vercel.app), con contraseña
  (`UI_PASSWORD`). Lee de Vercel Blob privado los resúmenes que sube GitHub Actions.
  `web/public/index.html` se genera: después de tocar `PAGE` en ui.py, correr
  `python3 web/scripts/build.py` y commitear el resultado.
- `.github/workflows/scrape.yml`: corrida cada 15 min (scrapea, avisa por Telegram, publica el resumen).
  Config real en el secret `CONFIG_JSON`; estado (`turbo.db`) en el cache de Actions.

## Producción

- **GitHub Actions** usa lo que está en `main`.
- **Vercel** publica `web/` en cada push a `main`; cada branch tiene un link de preview.
- En la Mac no corre nada automático. UI local a mano: `python3 ui.py`.
  No usar `scripts/launchd.sh install`: también prende el scraper local y duplica avisos.

## Flujo de cambios

1. Branch nueva desde `main` (nunca commitear directo en `main`).
2. Editar, correr los tests (`python3 -m unittest`; corren también en cada PR) y probar local: `python3 ui.py`, `python3 -m turbo run`, o la UI con una copia de la base
   (`python3 ui.py --db copia.db`).
3. Commit con mensaje en castellano, push, Pull Request. El dueño revisa y hace el merge.
4. Un tema por commit/PR.

## Privacidad (el repo es público)

Nunca commitear `config.json`, `data/`, `.env*`, direcciones, coordenadas reales, tokens ni el
chat_id de Telegram. Los logs de Actions también son públicos: el workflow enmascara direcciones.
El export para la UI no incluye nombres ni coordenadas de las direcciones.

## Si la API de Rappi falla

Revisar primero `app_version` en `config.json` (header `app-version` de rappi.com.ar en DevTools).
