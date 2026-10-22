"""
Cliente IPP mínimo (RFC 8010/8011) sin dependencias externas salvo httpx.

Permite:
  - print_job(pdf_bytes, ...)        -> envía el PDF y devuelve el job-id de la impresora
  - get_job_state(job_id)            -> estado real del trabajo en la impresora
  - get_printer_status()             -> estado de la impresora (lista, imprimiendo, sin papel, etc.)
  - cancel_job(job_id)
"""
import itertools
import struct

import httpx

# Operaciones IPP
OP_PRINT_JOB = 0x0002
OP_CANCEL_JOB = 0x0008
OP_GET_JOB_ATTRIBUTES = 0x0009
OP_GET_PRINTER_ATTRIBUTES = 0x000B

# Etiquetas de grupo
TAG_OPERATION = 0x01
TAG_JOB = 0x02
TAG_END = 0x03
TAG_PRINTER = 0x04

# Etiquetas de valor
TAG_INTEGER = 0x21
TAG_BOOLEAN = 0x22
TAG_ENUM = 0x23
TAG_TEXT = 0x41
TAG_NAME = 0x42
TAG_KEYWORD = 0x44
TAG_URI = 0x45
TAG_CHARSET = 0x47
TAG_LANGUAGE = 0x48
TAG_MIME = 0x49

JOB_STATES = {
    3: "pending",
    4: "pending-held",
    5: "processing",
    6: "processing-stopped",
    7: "canceled",
    8: "aborted",
    9: "completed",
}
PRINTER_STATES = {3: "idle", 4: "processing", 5: "stopped"}

_request_ids = itertools.count(1)


class IPPError(Exception):
    pass


def _attr(tag: int, name: str, value) -> bytes:
    if tag in (TAG_INTEGER, TAG_ENUM):
        raw = struct.pack(">i", int(value))
    elif tag == TAG_BOOLEAN:
        raw = b"\x01" if value else b"\x00"
    else:
        raw = str(value).encode("utf-8")
    n = name.encode("utf-8")
    return struct.pack(">BH", tag, len(n)) + n + struct.pack(">H", len(raw)) + raw


def _build(operation: int, printer_uri: str, op_attrs: list, job_attrs: list | None = None) -> bytes:
    body = struct.pack(">BBHI", 2, 0, operation, next(_request_ids))
    body += bytes([TAG_OPERATION])
    body += _attr(TAG_CHARSET, "attributes-charset", "utf-8")
    body += _attr(TAG_LANGUAGE, "attributes-natural-language", "es")
    body += _attr(TAG_URI, "printer-uri", printer_uri)
    for a in op_attrs:
        body += _attr(*a)
    if job_attrs:
        body += bytes([TAG_JOB])
        for a in job_attrs:
            body += _attr(*a)
    body += bytes([TAG_END])
    return body


def parse_response(data: bytes) -> tuple[int, dict]:
    """Devuelve (status_code, {nombre: valor o lista de valores})."""
    if len(data) < 8:
        raise IPPError("Respuesta IPP vacía o incompleta")
    status = struct.unpack(">H", data[2:4])[0]
    attrs: dict = {}
    pos, last = 8, None
    while pos < len(data):
        tag = data[pos]
        pos += 1
        if tag == TAG_END:
            break
        if tag < 0x10:  # delimitador de grupo
            continue
        name_len = struct.unpack(">H", data[pos:pos + 2])[0]
        pos += 2
        name = data[pos:pos + name_len].decode("utf-8", "replace")
        pos += name_len
        val_len = struct.unpack(">H", data[pos:pos + 2])[0]
        pos += 2
        raw = data[pos:pos + val_len]
        pos += val_len
        if tag in (TAG_INTEGER, TAG_ENUM) and val_len == 4:
            value = struct.unpack(">i", raw)[0]
        elif tag == TAG_BOOLEAN:
            value = raw == b"\x01"
        elif 0x40 <= tag <= 0x4F:
            value = raw.decode("utf-8", "replace")
        else:
            value = raw.hex()
        if name:  # atributo nuevo
            last = name
            attrs[name] = value
        elif last:  # valor adicional del atributo anterior (1setOf)
            prev = attrs[last]
            attrs[last] = (prev if isinstance(prev, list) else [prev]) + [value]
    return status, attrs


class IPPPrinter:
    def __init__(self, uri: str, timeout: float = 60.0, verify_tls: bool = False):
        # uri tipo ipp://192.168.1.134:631/ipp/print  -> se envía por HTTP
        self.printer_uri = uri
        http_url = uri.replace("ipps://", "https://").replace("ipp://", "http://")
        self.http_url = http_url
        self.timeout = timeout
        self.verify_tls = verify_tls

    def _send(self, payload: bytes) -> dict:
        r = httpx.post(
            self.http_url,
            content=payload,
            headers={"Content-Type": "application/ipp"},
            timeout=self.timeout,
            verify=self.verify_tls,
        )
        r.raise_for_status()
        status, attrs = parse_response(r.content)
        if status > 0x00FF:
            msg = attrs.get("status-message", "")
            raise IPPError(f"IPP status 0x{status:04x} {msg}".strip())
        return attrs

    def print_job(self, pdf: bytes, job_name: str, user: str = "sgit", copies: int = 1) -> int:
        payload = _build(
            OP_PRINT_JOB,
            self.printer_uri,
            [
                (TAG_NAME, "requesting-user-name", user),
                (TAG_NAME, "job-name", job_name[:200]),
                (TAG_MIME, "document-format", "application/pdf"),
            ],
            [(TAG_INTEGER, "copies", copies)] if copies > 1 else None,
        )
        attrs = self._send(payload + pdf)
        if "job-id" not in attrs:
            raise IPPError("La impresora no devolvió job-id")
        return int(attrs["job-id"])

    def get_job_state(self, job_id: int) -> dict:
        payload = _build(
            OP_GET_JOB_ATTRIBUTES,
            self.printer_uri,
            [
                (TAG_INTEGER, "job-id", job_id),
                (TAG_NAME, "requesting-user-name", "sgit"),
            ],
        )
        attrs = self._send(payload)
        state = attrs.get("job-state")
        reasons = attrs.get("job-state-reasons", [])
        return {
            "state": JOB_STATES.get(state, str(state)),
            "reasons": reasons if isinstance(reasons, list) else [reasons],
            "pages": attrs.get("job-impressions-completed") or attrs.get("job-media-sheets-completed"),
        }

    def cancel_job(self, job_id: int) -> None:
        payload = _build(
            OP_CANCEL_JOB,
            self.printer_uri,
            [(TAG_INTEGER, "job-id", job_id), (TAG_NAME, "requesting-user-name", "sgit")],
        )
        self._send(payload)

    def get_printer_status(self) -> dict:
        payload = _build(OP_GET_PRINTER_ATTRIBUTES, self.printer_uri, [])
        attrs = self._send(payload)
        reasons = attrs.get("printer-state-reasons", [])
        reasons = reasons if isinstance(reasons, list) else [reasons]
        return {
            "state": PRINTER_STATES.get(attrs.get("printer-state"), str(attrs.get("printer-state"))),
            "reasons": reasons,
            "accepting_jobs": attrs.get("printer-is-accepting-jobs"),
            "model": attrs.get("printer-make-and-model"),
            "message": attrs.get("printer-state-message"),
        }
