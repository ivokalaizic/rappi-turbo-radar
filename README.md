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

Solo se evalúan productos **nuevos o que cambiaron** en la corrida. Cada hallazgo se avisa **una vez
mientras dure**: si el precio se reajusta un poco (Rappi mueve ±1–2 % miles de productos por día) no se
repite; solo se vuelve a avisar si baja más de `realertar_si_baja` (5 %) respecto del último aviso, o si
la oferta terminó (el precio subió) y más adelante vuelve.

| Regla | Dispara cuando | Default |
|---|---|---|
| `precio_absurdo` | precio ≤ $X | $10 |
| `caida_vs_historial` | precio ≤ X × precio habitual (el que más tiempo tuvo en los últimos `historial_dias`) | 0.5 |
| `descuento_extremo` | precio ≤ X × precio de lista (tachado) | 0.2 |
| `vs_otras_tiendas` | precio ≤ X × mediana del mismo producto en otras tiendas Turbo | 0.5 |
| `gran_descuento` | oferta fuerte **y real** (no es error): precio ≤ X × precio de lista, solo si la oferta es ✅ real (ver abajo) | 0.5 |
| `nuevo_vs_pasillo` | producto **sin historial** (nuevo): precio por kg/L ≤ X × el de los más baratos (percentil 10) de su sub-pasillo | 0.2 |

`caida_vs_historial` necesita historia propia; `nuevo_vs_pasillo` cubre a los productos nuevos. Solo usa
peso y volumen (las "Und" no son comparables) y sub-pasillos con 8+ productos. Con 0.2 no dispara con
ningún producto del catálogo actual y detecta aproximadamente la mitad de los precios con un cero de menos.

### ¿El descuento es de verdad?

Rappi a veces infla el precio tachado para que el precio de siempre parezca una oferta. Por eso cada
descuento se clasifica contra **nuestro propio historial**, no contra el tachado (`detect.offer_status`):

| Etiqueta | Cuándo | Parámetro |
|---|---|---|
| ✅ Oferta real | bajó hace poco al menos X respecto de su precio habitual previo | `oferta_real_baja` = 0.15 |
| 🎭 Descuento inflado | cuesta lo mismo hace X días o más (es su precio normal), o no bajó respecto de antes | `oferta_permanente_dias` = 7 |
| ⏳ Sin historial | lo vemos hace menos de X días: todavía no se sabe | `oferta_min_historial_dias` = 7 |

Reajustes de ±2 % cuentan como el mismo precio. La clasificación mejora sola a medida que se junta historial.

Productos sin stock no alertan. Los que **desaparecen del catálogo** (Rappi saca lo que se queda sin
stock) se marcan sin stock en la primera corrida completa en que no vienen; si vuelven, se evalúan de nuevo.

**Promos para usuarios nuevos**: los productos con "Máx. 1 Ud." (p. ej. palta o huevos a $1) son promos
de bienvenida que **no aplican a cuentas existentes** (verificado con una cuenta real). Se excluyen
salvo que pongas `"include_new_user_promos": true`.

## Tests

`python3 -m unittest` (también corren solos en GitHub en cada Pull Request).

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

- `turbo.db` (SQLite): `products` (estado actual), `price_changes` (historial: una fila por cambio
  de precio, precio de lista, promo o stock), `alerts` (log de avisos), `alert_state` (hallazgos
  vigentes, para no repetir avisos), `runs`, `locations`.
- `session.json`: token de invitado (dura 7 días, se renueva solo).
- `turbo.log`.

## Si deja de funcionar

La API es interna y puede cambiar. Lo primero a revisar es `app_version` en `config.json`:
abrí rappi.com.ar con DevTools → Network → cualquier request a `services.rappi.com.ar`
→ header `app-version`.
