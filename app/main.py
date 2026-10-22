"""
Print API: recibe PDF por HTTP, los imprime en la HP E52645 por IPP y
expone el estado de cada trabajo.

Estados del trabajo (campo "status"):
  queued      -> recibido, esperando turno
  sending     -> enviándose a la impresora
  printing    -> la impresora lo aceptó y lo está procesando
  completed   -> la impresora reporta el trabajo como terminado (impreso)
  retrying    -> falló el envío, se reintentará
  failed      -> error definitivo (ver "error")
  canceled    -> cancelado por la API o en la impresora
"""
import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile

from .ipp import IPPError, IPPPrinter

# ---------------- Configuración ----------------
API_KEY = os.environ["API_KEY"]
PRINTER_URI = os.environ.get("PRINTER_URI", "ipp://192.168.1.134:631/ipp/print")
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
MAX_MB = int(os.environ.get("MAX_MB", "20"))
MAX_ATTEMPTS = int(os.environ.get("MAX_ATTEMPTS", "3"))
JOB_TIMEOUT_MIN = int(os.environ.get("JOB_TIMEOUT_MIN", "15"))
KEEP_FILES_HOURS = int(os.environ.get("KEEP_FILES_HOURS", "24"))

FILES_DIR = DATA_DIR / "files"
FILES_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "jobs.db"

TERMINAL = {"completed", "failed", "canceled"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("print-api")
logging.getLogger("httpx").setLevel(logging.WARNING)
printer = IPPPrinter(PRINTER_URI)
app = FastAPI(title="ZGROUP Print API", version="1.0")

# ---------------- Base de datos ----------------
_db_lock = threading.Lock()


@contextmanager
def db():
    with _db_lock:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init_db():
    with db() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                external_id TEXT UNIQUE,
                filename TEXT,
                sha256 TEXT,
                size_bytes INTEGER,
                copies INTEGER,
                status TEXT,
                printer_job_id INTEGER,
                printer_state TEXT,
                printer_reasons TEXT,
                pages INTEGER,
                attempts INTEGER DEFAULT 0,
                error TEXT,
                callback_url TEXT,
                created_at TEXT,
                updated_at TEXT,
                sent_at TEXT,
                completed_at TEXT
            )"""
        )


def update(job_id: str, **fields):
    fields["updated_at"] = now()
    cols = ", ".join(f"{k} = ?" for k in fields)
    with db() as c:
        c.execute(f"UPDATE jobs SET {cols} WHERE id = ?", (*fields.values(), job_id))


def get_job(job_id: str) -> dict | None:
    with db() as c:
        row = c.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return to_dict(row) if row else None


def to_dict(row) -> dict:
    d = dict(row)
    d["printer_reasons"] = json.loads(d["printer_reasons"]) if d["printer_reasons"] else []
    d.pop("sha256", None)
    return d


# ---------------- Seguridad ----------------
def require_key(x_api_key: str = Header(...)):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="API key inválida")


# ---------------- Endpoints ----------------
@app.get("/health")
def health():
    return {"ok": True}


@app.post("/jobs", status_code=202, dependencies=[Depends(require_key)])
async def create_job(
    file: UploadFile = File(..., description="PDF a imprimir"),
    external_id: str | None = Form(None, description="ID del documento en SGIT (evita duplicados)"),
    copies: int = Form(1, ge=1, le=20),
    callback_url: str | None = Form(None, description="URL a la que se notifica el estado final"),
):
    data = await file.read()
    if not data.startswith(b"%PDF"):
        raise HTTPException(400, "El archivo no es un PDF válido")
    if len(data) > MAX_MB * 1024 * 1024:
        raise HTTPException(413, f"El PDF supera {MAX_MB} MB")

    if external_id:
        with db() as c:
            row = c.execute("SELECT * FROM jobs WHERE external_id = ?", (external_id,)).fetchone()
        if row:  # idempotencia: el mismo documento no se imprime dos veces
            return {**to_dict(row), "duplicate": True}

    job_id = uuid.uuid4().hex
    (FILES_DIR / f"{job_id}.pdf").write_bytes(data)
    with db() as c:
        c.execute(
            """INSERT INTO jobs (id, external_id, filename, sha256, size_bytes, copies, status,
                                 callback_url, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?)""",
            (job_id, external_id, file.filename, hashlib.sha256(data).hexdigest(), len(data),
             copies, callback_url, now(), now()),
        )
    _wake.set()
    log.info("Trabajo %s en cola (%s, %d KB)", job_id, file.filename, len(data) // 1024)
    return get_job(job_id)


@app.get("/jobs/{job_id}", dependencies=[Depends(require_key)])
def read_job(job_id: str):
    job = get_job(job_id)
    if not job:
        raise HTTPException(404, "Trabajo no encontrado")
    return job


@app.get("/jobs", dependencies=[Depends(require_key)])
def list_jobs(external_id: str | None = None, status: str | None = None, limit: int = 50):
    q, args = "SELECT * FROM jobs WHERE 1=1", []
    if external_id:
        q += " AND external_id = ?"
        args.append(external_id)
    if status:
        q += " AND status = ?"
        args.append(status)
    q += " ORDER BY created_at DESC LIMIT ?"
    args.append(min(limit, 500))
    with db() as c:
        return [to_dict(r) for r in c.execute(q, args).fetchall()]


@app.delete("/jobs/{job_id}", dependencies=[Depends(require_key)])
def cancel_job(job_id: str):
    job = get_job(job_id)
    if not job:
        raise HTTPException(404, "Trabajo no encontrado")
    if job["status"] in TERMINAL:
        raise HTTPException(409, f"El trabajo ya está en estado {job['status']}")
    if job["printer_job_id"]:
        try:
            printer.cancel_job(job["printer_job_id"])
        except Exception as exc:
            raise HTTPException(502, f"No se pudo cancelar en la impresora: {exc}")
    update(job_id, status="canceled", completed_at=now())
    return get_job(job_id)


@app.get("/printer", dependencies=[Depends(require_key)])
def printer_status():
    try:
        return printer.get_printer_status()
    except Exception as exc:
        raise HTTPException(502, f"Impresora no disponible: {exc}")


# ---------------- Worker ----------------
_wake = threading.Event()


def notify(job: dict):
    if not job.get("callback_url"):
        return
    try:
        httpx.post(job["callback_url"], json=job, timeout=10)
    except Exception as exc:
        log.warning("Callback falló para %s: %s", job["id"], exc)


def finish(job_id: str, **fields):
    update(job_id, completed_at=now(), **fields)
    job = get_job(job_id)
    log.info("Trabajo %s -> %s", job_id, job["status"])
    notify(job)


def send_pending():
    with db() as c:
        row = c.execute(
            "SELECT * FROM jobs WHERE status IN ('queued', 'retrying') ORDER BY created_at LIMIT 1"
        ).fetchone()
    if not row:
        return False
    job = dict(row)
    path = FILES_DIR / f"{job['id']}.pdf"
    attempts = job["attempts"] + 1
    update(job["id"], status="sending", attempts=attempts)
    try:
        pid = printer.print_job(path.read_bytes(), job["filename"] or job["id"], copies=job["copies"])
        update(job["id"], status="printing", printer_job_id=pid, sent_at=now(), error=None)
        log.info("Trabajo %s enviado (job-id impresora %d)", job["id"], pid)
    except (IPPError, httpx.HTTPError, OSError) as exc:
        if attempts >= MAX_ATTEMPTS:
            finish(job["id"], status="failed", error=str(exc))
        else:
            update(job["id"], status="retrying", error=str(exc))
            time.sleep(10 * attempts)
    return True


def poll_printing():
    with db() as c:
        rows = c.execute("SELECT * FROM jobs WHERE status = 'printing'").fetchall()
    for row in rows:
        job = dict(row)
        try:
            st = printer.get_job_state(job["printer_job_id"])
        except Exception as exc:
            log.warning("No se pudo consultar %s: %s", job["id"], exc)
            continue
        fields = dict(printer_state=st["state"], printer_reasons=json.dumps(st["reasons"]), pages=st["pages"])
        if st["state"] == "completed":
            finish(job["id"], status="completed", **fields)
        elif st["state"] in ("canceled", "aborted"):
            finish(job["id"], status="canceled" if st["state"] == "canceled" else "failed",
                   error=f"Impresora: {st['state']} {st['reasons']}", **fields)
        else:
            sent = datetime.fromisoformat(job["sent_at"])
            if (datetime.now(timezone.utc) - sent).total_seconds() > JOB_TIMEOUT_MIN * 60:
                finish(job["id"], status="failed", error=f"Timeout: sigue en {st['state']} {st['reasons']}", **fields)
            else:
                update(job["id"], **fields)


def cleanup_files():
    limit = time.time() - KEEP_FILES_HOURS * 3600
    for f in FILES_DIR.glob("*.pdf"):
        if f.stat().st_mtime < limit:
            f.unlink(missing_ok=True)


def worker():
    last_cleanup = 0.0
    while True:
        try:
            while send_pending():
                pass
            poll_printing()
            if time.time() - last_cleanup > 3600:
                cleanup_files()
                last_cleanup = time.time()
        except Exception:
            log.exception("Error en el worker")
        _wake.wait(timeout=3)
        _wake.clear()


@app.on_event("startup")
def startup():
    init_db()
    # si el servicio se reinició a mitad de un envío, se reintenta
    with db() as c:
        c.execute("UPDATE jobs SET status = 'queued' WHERE status = 'sending'")
    threading.Thread(target=worker, daemon=True).start()
    log.info("Print API lista. Impresora: %s", PRINTER_URI)
