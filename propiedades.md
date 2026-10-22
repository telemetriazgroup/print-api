# Propiedades — ZGROUP Print API

API HTTP que recibe PDF y los imprime en la **HP LaserJet MFP E52645** por **IPP**, con cola, reintentos y seguimiento real del estado de cada trabajo.

```
SGIT ──HTTPS──▶ Print API (192.168.1.166:7101) ──IPP:631──▶ HP E52645 (192.168.1.134)
```

## Identidad

| Propiedad | Valor |
|---|---|
| Nombre | ZGROUP Print API |
| Versión | 1.0 |
| Lenguaje | Python 3.12 |
| Framework | FastAPI 0.115 + Uvicorn |
| Protocolo de impresión | IPP (RFC 8010 / 8011), cliente propio en `app/ipp.py` |
| Base de datos | SQLite (`data/jobs.db`) |
| Empaquetado | Docker (`python:3.12-slim`), Compose, puerto **7101** |
| Consumidor previsto | SGIT |

## Qué hace

1. Recibe un PDF por `POST /jobs` (multipart).
2. Valida que empiece por `%PDF` y que no supere `MAX_MB`.
3. Lo guarda en disco y lo deja en cola (`queued`).
4. Un worker en segundo plano lo envía a la impresora (`Print-Job`).
5. Consulta el estado real del trabajo en la impresora cada pocos segundos.
6. Al terminar (`completed`, `failed` o `canceled`) notifica a `callback_url` si se indicó.

Un solo trabajo se envía a la vez. El worker despierta cada 3 s, o de inmediato cuando entra un PDF nuevo.

## Endpoints

Todos exigen el header `X-API-Key`, excepto `/health`.

| Método | Ruta | Respuesta | Uso |
|---|---|---|---|
| `GET` | `/health` | 200 | Vida del servicio. Sin autenticación. |
| `POST` | `/jobs` | 202 | Encola un PDF. Campos: `file` (obligatorio), `external_id`, `copies` (1–20), `callback_url`. |
| `GET` | `/jobs/{id}` | 200 / 404 | Estado de un trabajo. |
| `GET` | `/jobs` | 200 | Lista. Filtros: `external_id`, `status`. `limit` hasta 500 (por defecto 50). |
| `DELETE` | `/jobs/{id}` | 200 / 409 / 502 | Cancela si aún no terminó. Si ya está en la impresora, envía `Cancel-Job`. |
| `GET` | `/printer` | 200 / 502 | Estado de la impresora: `idle`, `processing`, `stopped`, más motivos y si acepta trabajos. |

Documentación interactiva: `http://192.168.1.166:7101/docs`.

## Estados de un trabajo

| status | Significado |
|---|---|
| `queued` | Recibido, esperando turno. |
| `sending` | Enviándose a la impresora. Si el proceso se reinicia en este estado, vuelve a `queued`. |
| `retrying` | Falló el envío (red, impresora apagada). Se reintenta hasta `MAX_ATTEMPTS`, con espera de `10 × intento` segundos. |
| `printing` | La impresora lo aceptó y lo está procesando. |
| `completed` | La impresora reporta el trabajo como terminado. |
| `failed` | Error definitivo: se agotaron los reintentos, la impresora abortó el trabajo, o superó `JOB_TIMEOUT_MIN`. |
| `canceled` | Cancelado desde la API o en el panel de la impresora. |

Estados finales: `completed`, `failed`, `canceled`. Al llegar a uno de ellos se hace `POST` del JSON del trabajo a `callback_url` (timeout 10 s). Si el callback falla, solo se registra en el log.

Campos útiles de la respuesta: `printer_job_id`, `printer_state`, `printer_reasons`, `pages`, `attempts`, `error`, `sent_at`, `completed_at`. `sha256` se guarda en la base y no se devuelve en la API.

Si se reenvía el mismo `external_id`, no se imprime de nuevo: se devuelve el trabajo existente con `"duplicate": true`.

## Impresora

| Propiedad | Valor |
|---|---|
| Modelo | HP LaserJet MFP E52645 |
| URI por defecto | `ipp://192.168.1.134:631/ipp/print` |
| Transporte | HTTP POST con `Content-Type: application/ipp` |
| Usuario IPP | `sgit` |
| Operaciones | Print-Job, Get-Job-Attributes, Get-Printer-Attributes, Cancel-Job |
| Timeout de cada llamada IPP | 60 s |
| TLS | `verify_tls` desactivado (la URI por defecto es `ipp://`, no `ipps://`) |

Estados IPP del trabajo que entiende el cliente: `pending`, `pending-held`, `processing`, `processing-stopped`, `canceled`, `aborted`, `completed`.

Estados de la impresora: `idle`, `processing`, `stopped`. Motivos típicos en `reasons`: `media-empty`, `media-jam`, `toner-low`, `door-open`. Un atasco o falta de papel deja el trabajo en `printing` hasta que termine o venza `JOB_TIMEOUT_MIN`.

## Configuración (`.env`)

| Variable | Defecto | Efecto |
|---|---|---|
| `API_KEY` | obligatoria | Clave del header `X-API-Key`. Si falta, el proceso no arranca. |
| `PRINTER_URI` | `ipp://192.168.1.134:631/ipp/print` | Destino IPP. |
| `DATA_DIR` | `/data` | PDF y SQLite. En Compose se monta `./data`. |
| `MAX_MB` | 20 | Tamaño máximo del PDF. Por encima responde 413. |
| `MAX_ATTEMPTS` | 3 | Reintentos de envío antes de `failed`. |
| `JOB_TIMEOUT_MIN` | 15 | Minutos en `printing` antes de marcarlo `failed`. |
| `KEEP_FILES_HOURS` | 24 | Los PDF se borran pasado este tiempo. El historial en SQLite permanece. |

## Persistencia

Tabla `jobs` en SQLite: `id` (UUID hex), `external_id` (único), `filename`, `sha256`, `size_bytes`, `copies`, `status`, `printer_job_id`, `printer_state`, `printer_reasons` (JSON), `pages`, `attempts`, `error`, `callback_url`, `created_at`, `updated_at`, `sent_at`, `completed_at`.

El acceso a la base va serializado con un lock: una conexión por operación. Los PDF viven en `{DATA_DIR}/files/{id}.pdf`. Una limpieza horaria borra los que superan `KEEP_FILES_HOURS`.

## Seguridad y red

- Autenticación por API key fija en header. Comparación directa, sin caducidad ni roles.
- La impresora no se expone: solo el host de la API (192.168.1.166) habla con el puerto 631.
- En la misma LAN basta la API key. Desde internet el diseño previsto es túnel (Cloudflare, WireGuard o Tailscale), o proxy HTTPS con lista de IP, sin abrir el 7101 en el router.
- La red Docker usa `10.150.0.0/24` para no solaparse con `192.168.1.0/24`.
- El contenedor reinicia solo (`unless-stopped`).

## Límites conocidos

- Cola de un solo envío a la vez: los PDF esperan turno en orden de llegada.
- No hay usuarios ni permisos: una sola clave abre todos los endpoints.
- `callback_url` se llama sin firma ni reintento.
- `completed` significa que la impresora dio el trabajo por terminado, no que alguien recogió el papel.
- El cliente IPP cubre solo las cuatro operaciones que usa esta API.
