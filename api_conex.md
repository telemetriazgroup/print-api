# Conexión y prueba de sincronización

Guía para que otra aplicación compruebe que la Print API está en línea, envíe un PDF de prueba y confirme que el trabajo quedó registrado y terminó en la impresora.

## Datos de conexión

| Dato | Valor |
|---|---|
| Base URL | `http://192.168.1.166:7101` |
| Puerto | `7101` |
| Autenticación | Header `X-API-Key` con la clave definida en `.env` |
| Formato de envío | `multipart/form-data` |
| Documento | PDF (`application/pdf`). Debe empezar por `%PDF` y no superar 20 MB |
| Swagger | `http://192.168.1.166:7101/docs` |

`/health` no pide clave. El resto de rutas responde `401` si la clave falta o no coincide.

Sustituye `TU_CLAVE` por el valor real de `API_KEY`. En los ejemplos, `BASE` es `http://192.168.1.166:7101`.

## 1. Comprobar que está en línea

```bash
curl -sS http://192.168.1.166:7101/health
```

Respuesta esperada:

```json
{"ok": true}
```

Si no hay respuesta, el servicio no está levantado o el puerto 7101 no es alcanzable desde la aplicación.

## 2. Comprobar la clave y la impresora

```bash
curl -sS -H "X-API-Key: TU_CLAVE" http://192.168.1.166:7101/printer
```

Respuesta esperada (ejemplo):

```json
{
  "state": "idle",
  "reasons": ["none"],
  "accepting_jobs": true,
  "model": "HP LaserJet MFP E52645",
  "message": ""
}
```

La sincronización con la impresora es correcta cuando `state` es `idle` o `processing` y `accepting_jobs` es `true`. Si la API responde `502`, el servicio está vivo pero no llega a la impresora.

## 3. Enviar un documento de prueba

Usa un `external_id` único por prueba, por ejemplo `PRUEBA-20261022-001`. Ese valor es el identificador del documento en la aplicación que se conecta. Si se reenvía el mismo, la API no imprime de nuevo y devuelve el trabajo ya existente con `"duplicate": true`.

```bash
curl -sS -X POST http://192.168.1.166:7101/jobs \
  -H "X-API-Key: TU_CLAVE" \
  -F "file=@prueba.pdf" \
  -F "external_id=PRUEBA-20261022-001" \
  -F "copies=1"
```

Respuesta `202` (ejemplo):

```json
{
  "id": "b11f41c0e8a14d0a9c3e7f2a1b0d4e66",
  "external_id": "PRUEBA-20261022-001",
  "filename": "prueba.pdf",
  "size_bytes": 48211,
  "copies": 1,
  "status": "queued",
  "printer_job_id": null,
  "printer_state": null,
  "printer_reasons": [],
  "pages": null,
  "attempts": 0,
  "error": null,
  "callback_url": null,
  "created_at": "2026-10-22T19:00:00+00:00",
  "updated_at": "2026-10-22T19:00:00+00:00",
  "sent_at": null,
  "completed_at": null
}
```

Guarda `id`. Es el identificador del trabajo en esta API.

Campos del formulario:

| Campo | Obligatorio | Uso |
|---|---|---|
| `file` | sí | PDF a imprimir |
| `external_id` | no | ID del documento en la aplicación origen. Evita duplicados |
| `copies` | no | Copias, de 1 a 20. Por defecto 1 |
| `callback_url` | no | URL a la que la API hace `POST` con el JSON del trabajo al terminar |

Errores de envío:

| HTTP | Causa |
|---|---|
| 400 | El archivo no es un PDF |
| 401 | API key inválida |
| 413 | El PDF supera el máximo (20 MB) |

## 4. Ver el resultado

Consulta el mismo `id` cada 3–5 segundos hasta que `status` sea `completed`, `failed` o `canceled`.

```bash
curl -sS -H "X-API-Key: TU_CLAVE" \
  http://192.168.1.166:7101/jobs/b11f41c0e8a14d0a9c3e7f2a1b0d4e66
```

También se puede buscar por el ID de la aplicación:

```bash
curl -sS -H "X-API-Key: TU_CLAVE" \
  "http://192.168.1.166:7101/jobs?external_id=PRUEBA-20261022-001"
```

La respuesta es una lista. El trabajo de la prueba es el primer elemento si ese `external_id` es único.

### Cómo saber que la sincronización es correcta

La prueba queda bien cuando se cumplen las tres condiciones:

1. `POST /jobs` respondió `202` y devolvió un `id`.
2. `GET /jobs/{id}` (o la búsqueda por `external_id`) devuelve ese mismo trabajo, con el mismo `external_id` y `filename`.
3. `status` pasa a `completed`, `printer_job_id` tiene un número y `error` es `null`.

Ejemplo de trabajo sincronizado:

```json
{
  "id": "b11f41c0e8a14d0a9c3e7f2a1b0d4e66",
  "external_id": "PRUEBA-20261022-001",
  "filename": "prueba.pdf",
  "status": "completed",
  "printer_job_id": 42,
  "printer_state": "completed",
  "printer_reasons": ["none"],
  "pages": 1,
  "attempts": 1,
  "error": null,
  "sent_at": "2026-10-22T19:00:02+00:00",
  "completed_at": "2026-10-22T19:00:18+00:00"
}
```

`completed` significa que la impresora reportó el trabajo como terminado. El papel debe haber salido de la HP E52645.

Si `status` es `failed`, la sincronización con la API ocurrió (el trabajo existe) pero la impresión no. Revisa `error` y `printer_reasons` (`media-empty`, `media-jam`, `door-open`, timeout).

Recorrido normal del estado: `queued` → `sending` → `printing` → `completed`. Si la impresora no responde al envío aparece `retrying` y, tras 3 intentos, `failed`.

## 5. Ejemplo mínimo para la aplicación

```js
const BASE = "http://192.168.1.166:7101";
const API_KEY = process.env.PRINT_API_KEY;
const headers = { "X-API-Key": API_KEY };

const health = await fetch(`${BASE}/health`).then((r) => r.json());
if (!health.ok) throw new Error("Print API fuera de línea");

const form = new FormData();
form.append("file", new Blob([pdfBuffer], { type: "application/pdf" }), "prueba.pdf");
form.append("external_id", "PRUEBA-20261022-001");
form.append("copies", "1");

const created = await fetch(`${BASE}/jobs`, { method: "POST", headers, body: form });
if (!created.ok) throw new Error(await created.text());
const job = await created.json();

const terminal = new Set(["completed", "failed", "canceled"]);
let current = job;
while (!terminal.has(current.status)) {
  await new Promise((r) => setTimeout(r, 4000));
  const res = await fetch(`${BASE}/jobs/${job.id}`, { headers });
  current = await res.json();
}

// Sincronización correcta: mismo documento, impreso, sin error
const ok =
  current.external_id === "PRUEBA-20261022-001" &&
  current.status === "completed" &&
  current.printer_job_id != null &&
  current.error == null;
```

## Cancelar una prueba

Solo si el trabajo aún no terminó:

```bash
curl -sS -X DELETE -H "X-API-Key: TU_CLAVE" \
  http://192.168.1.166:7101/jobs/b11f41c0e8a14d0a9c3e7f2a1b0d4e66
```

Responde `409` si ya está en `completed`, `failed` o `canceled`.
