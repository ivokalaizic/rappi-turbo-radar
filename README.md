# Rappi Turbo — detector de precios mal puestos

Recorre el catálogo completo de las tiendas **Rappi Turbo** que atienden tus direcciones
y avisa cuando un producto aparece con un precio absurdamente bajo.

- Sin cuenta: usa la API web de Rappi como invitado.
- Sin dependencias: Python 3.10+ y la librería estándar.
- Varias direcciones: las que caen en la misma tienda se scrapean una sola vez.

## Uso

```bash
python3 ui.py                        # UI web: dirección → corre el scraper → ofertas y precios raros
cp config.example.json config.json   # cargá tus direcciones (lat/lng)
python3 -m turbo stores              # qué tienda Turbo atiende cada dirección
python3 -m turbo run                 # una corrida completa (~2 min por tienda)
python3 -m turbo alerts --hours 48   # alertas recientes
scripts/launchd.sh install           # macOS: corrida cada 15 min + UI siempre prendida + avisos
```

La UI (`ui.py`, un solo archivo, sin dependencias) abre http://127.0.0.1:8765: cargás la
dirección y los umbrales, corre el scraper con barra de progreso (o reusa una corrida de menos
de 15 min) y lista los productos sospechosos o en descuento, con detalle e historial de precio.
Para probarla sin tocar tu base: `python3 ui.py --db copia.db`.

Para sacar la lat/lng de una dirección: Google Maps → click derecho sobre el punto → copiar coordenadas.

## Reglas de detección (`config.json` → `rules`)

Solo se evalúan productos **nuevos o que cambiaron de precio** en la corrida, así que un mismo
hallazgo no se repite cada 15 minutos. Además, cada alerta se emite una sola vez por producto y precio.

| Regla | Dispara cuando | Default |
|---|---|---|
| `precio_absurdo` | precio ≤ $X | $10 |
| `caida_vs_historial` | precio ≤ X × precio habitual (el que más tiempo tuvo en los últimos `historial_dias`) | 0.5 |
| `descuento_extremo` | precio ≤ X × precio de lista (tachado) | 0.2 |
| `vs_otras_tiendas` | precio ≤ X × mediana del mismo producto en otras tiendas Turbo | 0.5 |
| `gran_descuento` | oferta fuerte (no es error): precio ≤ X × precio de lista | 0.5 |

Productos sin stock no alertan.

**Promos para usuarios nuevos**: los productos con "Máx. 1 Ud." (p. ej. palta o huevos a $1) son promos
de bienvenida que **no aplican a cuentas existentes** (verificado con una cuenta real). Se excluyen
salvo que pongas `"include_new_user_promos": true`.

## Avisos (`config.json` → `notify`)

Al terminar cada corrida, las alertas nuevas se avisan una sola vez:

- **macOS** (`"macos": true`): cartel con los productos y botón «Ver ofertas», que abre la UI en
  esos hallazgos (`ui_url`). La corrida solo pasa mientras la Mac está despierta.
- **Telegram** (opcional): creá un bot con @BotFather, pegá el token en `telegram.bot_token`,
  mandale un mensaje al bot y poné tu `chat_id` (lo ves en
  `https://api.telegram.org/bot<TOKEN>/getUpdates`).

Para probar Telegram: `python3 -m turbo notify-test` (o en GitHub: Run workflow → «Mandar un aviso de prueba»).

## En la nube, gratis (GitHub Actions)

`.github/workflows/scrape.yml` corre el scraper cada 15 min aunque la Mac esté cerrada. Es gratis
e ilimitado si el repo es **público**; las direcciones y el token de Telegram van en un secret, nunca en el repo.

1. Subí el repo a GitHub como público.
2. Settings → Secrets and variables → Actions → New repository secret: `CONFIG_JSON` con tu
   `config.json` completo (con `telegram.bot_token` y `chat_id`; `macos` no aplica en la nube).
3. Actions → scrape → Run workflow para probarlo. La primera corrida arma la base sin avisar
   (`--baseline`); desde la segunda llegan los avisos por Telegram.

El estado (`turbo.db`, `session.json`) queda en el cache de Actions, que no es público. Los logs
sí lo son: el workflow oculta nombres y coordenadas de las direcciones. GitHub puede atrasar el
cron unos minutos.

## Datos

Todo queda en `data/` (ignorado por git):

- `turbo.db` (SQLite): `products` (estado actual), `price_changes` (historial, una fila por cambio),
  `alerts`, `runs`, `locations`.
- `session.json`: token de invitado (dura 7 días, se renueva solo).
- `turbo.log`.

## Si deja de funcionar

La API es interna y puede cambiar. Lo primero a revisar es `app_version` en `config.json`:
abrí rappi.com.ar con DevTools → Network → cualquier request a `services.rappi.com.ar`
→ header `app-version`.
