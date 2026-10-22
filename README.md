# ZGROUP Print API

API que recibe PDF por HTTP y los imprime en la **HP LaserJet MFP E52645** (192.168.1.134) usando **IPP**, con seguimiento real del estado de cada trabajo.

```
SGIT (otro servidor) ──HTTPS──▶ Print API (192.168.1.166:7101) ──IPP:631──▶ HP E52645 (192.168.1.134)
```

## Instalación en el servidor 192.168.1.166

```bash
# 1. Copiar la carpeta print-api al servidor
cd ~/print-api
cp .env.example .env
openssl rand -hex 32            # pega el resultado en API_KEY dentro de .env
nano .env

# 2. Verificar que el servidor llega a la impresora por IPP
nc -zv 192.168.1.134 631

# 3. Levantar
docker compose up -d --build
docker logs -f print-api

# 4. Probar
curl -H "X-API-Key: TU_CLAVE" http://192.168.1.166:7101/printer
```

Documentación interactiva (Swagger): `http://192.168.1.166:7101/docs`

## Endpoints

Todos requieren el header `X-API-Key`, excepto `/health`.

| Método | Ruta | Uso |
|---|---|---|
| `POST` | `/jobs` | Enviar un PDF (multipart). Campos: `file` (obligatorio), `external_id`, `copies`, `callback_url` |
| `GET` | `/jobs/{id}` | Estado de un trabajo |
| `GET` | `/jobs?external_id=GUIA-001` | Buscar por el ID del documento en SGIT |
| `GET` | `/jobs?status=failed` | Listar por estado |
| `DELETE` | `/jobs/{id}` | Cancelar (si aún no terminó) |
| `GET` | `/printer` | Estado de la impresora (idle, processing, stopped + motivos: `media-empty`, `toner-low`, `door-open`…) |
| `GET` | `/health` | Verificación de vida |

### Estados de un trabajo

| status | Significado |
|---|---|
| `queued` | Recibido, esperando turno |
| `sending` | Enviándose a la impresora |
| `retrying` | Falló el envío (impresora apagada, sin red); se reintenta hasta `MAX_ATTEMPTS` |
| `printing` | La impresora lo aceptó y lo está procesando |
| `completed` | La impresora reporta el trabajo como **terminado** |
| `failed` | Error definitivo. Ver `error` y `printer_reasons` |
| `canceled` | Cancelado desde la API o en el panel de la impresora |

Campos útiles: `printer_job_id` (ID en la impresora), `printer_state`, `printer_reasons`, `pages`, `attempts`, `error`, `sent_at`, `completed_at`.

## Ejemplos para SGIT

**curl**

```bash
curl -X POST http://192.168.1.166:7101/jobs \
  -H "X-API-Key: TU_CLAVE" \
  -F "file=@guia_001.pdf" \
  -F "external_id=GUIA-001" \
  -F "copies=2"
# -> {"id": "b11f41...", "status": "queued", ...}

curl -H "X-API-Key: TU_CLAVE" http://192.168.1.166:7101/jobs/b11f41...
```

**Node.js (fetch, Node 18+)**

```js
const form = new FormData();
form.append("file", new Blob([pdfBuffer], { type: "application/pdf" }), "guia_001.pdf");
form.append("external_id", "GUIA-001");
form.append("callback_url", "https://sgit.zgroup.com.pe/api/print-callback"); // opcional

const res = await fetch("http://192.168.1.166:7101/jobs", {
  method: "POST",
  headers: { "X-API-Key": process.env.PRINT_API_KEY },
  body: form,
});
const job = await res.json(); // job.id, job.status
```

**Seguimiento:** consulta `GET /jobs/{id}` cada 3–5 s hasta que `status` sea `completed`, `failed` o `canceled`; o usa `callback_url` y la API hará un `POST` con el JSON del trabajo cuando termine.

**Idempotencia:** si envías dos veces el mismo `external_id`, no se imprime de nuevo; la API devuelve el trabajo existente con `"duplicate": true`.

## Seguridad: cómo llega SGIT a esta API

- **SGIT en la misma red (192.168.1.x):** basta con la API key.
- **SGIT en otro lugar (internet o nube):** no abras el puerto 7101 en el router. Usa una de estas opciones:
  - **Cloudflare Tunnel** (`cloudflared`) en el 192.168.1.166: da una URL HTTPS pública sin abrir puertos.
  - **WireGuard / Tailscale** entre el servidor de SGIT y el 192.168.1.166.
  - Si abres el puerto sí o sí: proxy inverso con HTTPS (Nginx/Caddy) + API key + lista de IP permitidas (solo la IP del servidor SGIT).
- La impresora nunca queda expuesta: solo el 192.168.1.166 habla con ella.

## Notas

- `completed` significa que la impresora reporta el trabajo terminado. Si se atasca el papel o falta papel, el trabajo queda en `printing` con `printer_reasons` como `media-jam` o `media-empty`, y pasa a `failed` si supera `JOB_TIMEOUT_MIN`.
- Los PDF se borran del servidor después de `KEEP_FILES_HOURS`; el historial queda en `data/jobs.db` (SQLite).
- La red Docker del servicio usa `10.150.0.0/24` para no chocar con la LAN `192.168.1.x`.
