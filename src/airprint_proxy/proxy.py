"""IPP proxy server that accepts print jobs and forwards them as PWG Raster."""

import http.client
import logging
import struct
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

from .config import PrinterConfig
from .converter import convert_to_pwg_raster
from .ipp import (
    IPPResponseBuilder, parse_ipp_request,
    OP_GET_PRINTER_ATTRIBUTES, OP_PRINT_JOB, OP_VALIDATE_JOB,
    OP_CREATE_JOB, OP_SEND_DOCUMENT, OP_GET_JOBS, OP_CANCEL_JOB,
    OP_GET_JOB_ATTRIBUTES,
    STATUS_OK, STATUS_CLIENT_BAD_REQUEST, STATUS_SERVER_ERROR,
    TAG_OPERATION, TAG_PRINTER, TAG_JOB,
    VTAG_INTEGER, VTAG_BOOLEAN, VTAG_ENUM, VTAG_COLLECTION,
    VTAG_TEXT, VTAG_NAME, VTAG_KEYWORD, VTAG_URI, VTAG_CHARSET,
    VTAG_LANGUAGE, VTAG_MIME,
)

log = logging.getLogger(__name__)


class ProxyState:
    """Shared state for the proxy server."""
    def __init__(self, printer: PrinterConfig, proxy_host: str, proxy_port: int):
        self.printer = printer
        self.proxy_host = proxy_host
        self.proxy_port = proxy_port
        self.job_counter = 0
        self.lock = threading.Lock()
        # Pending job data for Create-Job + Send-Document flow
        self.pending_jobs: dict[int, dict] = {}

    def next_job_id(self) -> int:
        with self.lock:
            self.job_counter += 1
            return self.job_counter

    @property
    def proxy_uri(self) -> str:
        return f"ipp://{self.proxy_host}:{self.proxy_port}/ipp/print"


class IPPRequestHandler(BaseHTTPRequestHandler):
    """Handle IPP requests over HTTP POST."""

    protocol_version = "HTTP/1.1"
    server: "IPPProxyServer"

    def log_message(self, format, *args):
        log.info(format, *args)

    def do_POST(self):
        # Handle both Content-Length and chunked transfer encoding
        transfer_encoding = self.headers.get("Transfer-Encoding", "")
        content_length = int(self.headers.get("Content-Length", 0))

        if "chunked" in transfer_encoding.lower():
            raw = self._read_chunked()
        elif content_length:
            raw = self.rfile.read(content_length)
        else:
            raw = b""

        try:
            req = parse_ipp_request(raw)
        except Exception as e:
            log.error("Failed to parse IPP request: %s", e)
            self._send_ipp_error(STATUS_CLIENT_BAD_REQUEST, 1)
            return

        log.info("IPP operation: 0x%04x, request_id: %d", req.operation, req.request_id)

        try:
            if req.operation == OP_GET_PRINTER_ATTRIBUTES:
                self._handle_get_printer_attributes(req)
            elif req.operation == OP_PRINT_JOB:
                self._handle_print_job(req)
            elif req.operation == OP_VALIDATE_JOB:
                self._handle_validate_job(req)
            elif req.operation == OP_CREATE_JOB:
                self._handle_create_job(req)
            elif req.operation == OP_SEND_DOCUMENT:
                self._handle_send_document(req)
            elif req.operation == OP_GET_JOBS:
                self._handle_get_jobs(req)
            elif req.operation == OP_GET_JOB_ATTRIBUTES:
                self._handle_get_job_attributes(req)
            elif req.operation == OP_CANCEL_JOB:
                self._handle_cancel_job(req)
            else:
                log.warning("Unsupported operation: 0x%04x", req.operation)
                self._send_ipp_error(STATUS_SERVER_ERROR, req.request_id)
        except Exception:
            log.exception("Error handling IPP request")
            self._send_ipp_error(STATUS_SERVER_ERROR, req.request_id)

    def _read_chunked(self) -> bytes:
        """Read HTTP chunked transfer encoding."""
        data = b""
        while True:
            line = self.rfile.readline().strip()
            if not line:
                continue
            chunk_size = int(line, 16)
            if chunk_size == 0:
                self.rfile.readline()  # trailing CRLF
                break
            chunk = self.rfile.read(chunk_size)
            data += chunk
            self.rfile.readline()  # trailing CRLF after chunk
        return data

    def do_GET(self):
        log.info("GET %s from %s", self.path, self.client_address)
        # macOS may query / or /ipp/print via GET
        if self.path == "/ipp/print" or self.path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            state = self.server.state
            self.wfile.write(f"<html><body><h1>{state.printer.name}</h1>"
                            f"<p>AirPrint proxy</p>"
                            f"</body></html>".encode())
        else:
            self.send_response(404)
            self.end_headers()

    def do_HEAD(self):
        log.info("HEAD %s from %s", self.path, self.client_address)
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()

    def do_OPTIONS(self):
        log.info("OPTIONS %s from %s", self.path, self.client_address)
        self.send_response(200)
        self.send_header("Allow", "GET, HEAD, POST, OPTIONS")
        self.end_headers()

    def _send_ipp_response(self, data: bytes):
        self.send_response(200)
        self.send_header("Content-Type", "application/ipp")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_ipp_error(self, status: int, request_id: int):
        resp = IPPResponseBuilder(request_id, status)
        resp.start_group(TAG_OPERATION)
        resp.add_string(VTAG_CHARSET, "attributes-charset", "utf-8")
        resp.add_string(VTAG_LANGUAGE, "attributes-natural-language", "en")
        self._send_ipp_response(resp.build())

    def _handle_get_printer_attributes(self, req):
        state = self.server.state
        printer = state.printer
        resp = IPPResponseBuilder(req.request_id, STATUS_OK)

        resp.start_group(TAG_OPERATION)
        resp.add_string(VTAG_CHARSET, "attributes-charset", "utf-8")
        resp.add_string(VTAG_LANGUAGE, "attributes-natural-language", "en")

        resp.start_group(TAG_PRINTER)

        # Identity
        resp.add_string(VTAG_TEXT, "printer-info", printer.name)
        resp.add_string(VTAG_NAME, "printer-name", "ipp/print")
        resp.add_string(VTAG_TEXT, "printer-make-and-model", printer.make_and_model)
        resp.add_string(VTAG_TEXT, "printer-location", "")
        resp.add_string(VTAG_URI, "printer-uri-supported", state.proxy_uri)
        resp.add_string(VTAG_KEYWORD, "uri-security-supported", "none")
        resp.add_string(VTAG_KEYWORD, "uri-authentication-supported", "none")

        if printer.uuid:
            resp.add_string(VTAG_URI, "printer-uuid", f"urn:uuid:{printer.uuid}")

        # State
        resp.add_enum("printer-state", 3)  # idle
        resp.add_string(VTAG_KEYWORD, "printer-state-reasons", "none")
        resp.add_boolean("printer-is-accepting-jobs", True)
        resp.add_integer("queued-job-count", 0)

        # Capabilities - advertise formats macOS/iOS want
        pdl = ["image/urf", "image/pwg-raster", "application/pdf"]
        resp.add_strings(VTAG_MIME, "document-format-supported", pdl)
        resp.add_string(VTAG_MIME, "document-format-default", "application/octet-stream")

        # URF capabilities - tell macOS we support URF
        # Each capability token is a separate keyword value (1setOf keyword)
        urf_res = printer.pwg_raster_resolutions[0] if printer.pwg_raster_resolutions else 360
        urf_tokens = ["CP1", "MT1-2-8", f"RS{urf_res}", "SRGB24", "W8", "OB10", "PQ3-4-5"]
        if not printer.duplex:
            urf_tokens.append("DM1")
        else:
            urf_tokens.append("DM1-3")
        resp.add_strings(VTAG_KEYWORD, "urf-supported", urf_tokens)

        # PWG Raster capabilities
        if printer.pwg_raster_types:
            resp.add_strings(VTAG_KEYWORD, "pwg-raster-document-type-supported",
                           printer.pwg_raster_types)
        else:
            resp.add_strings(VTAG_KEYWORD, "pwg-raster-document-type-supported",
                           ["sgray_8", "srgb_8"])

        resp.add_resolution("pwg-raster-document-resolution-supported", urf_res, urf_res)
        resp.add_string(VTAG_KEYWORD, "pwg-raster-document-sheet-back", "rotated")

        # PDF
        resp.add_string(VTAG_KEYWORD, "pdf-versions-supported", "iso-32000-1_2008")

        # Color
        resp.add_boolean("color-supported", printer.color)
        colors = ["color", "monochrome", "auto"] if printer.color else ["monochrome"]
        resp.add_strings(VTAG_KEYWORD, "print-color-mode-supported", colors)
        resp.add_string(VTAG_KEYWORD, "print-color-mode-default", "auto" if printer.color else "monochrome")

        # Duplex
        sides = ["one-sided"]
        if printer.duplex:
            sides.extend(["two-sided-long-edge", "two-sided-short-edge"])
        resp.add_strings(VTAG_KEYWORD, "sides-supported", sides)
        resp.add_string(VTAG_KEYWORD, "sides-default", "one-sided")

        # Copies
        resp.add_attribute(0x33, "copies-supported", struct.pack(">ii", 1, 99))
        resp.add_integer("copies-default", 1)

        # Quality
        resp.add_enum("print-quality-default", 4)  # normal
        resp.add_enum("print-quality-supported", 4)
        resp.add_additional_value(VTAG_ENUM, struct.pack(">i", 5))  # high

        # Media
        resp.add_string(VTAG_KEYWORD, "media-default", "iso_a4_210x297mm")
        media = [
            "iso_a4_210x297mm", "na_letter_8.5x11in", "na_legal_8.5x14in",
            "iso_a5_148x210mm", "na_index-4x6_4x6in", "na_5x7_5x7in",
        ]
        resp.add_strings(VTAG_KEYWORD, "media-supported", media)

        # Resolution
        resp.add_resolution("printer-resolution-default", urf_res, urf_res)
        resp.add_resolution("printer-resolution-supported", urf_res, urf_res)

        # IPP versions
        resp.add_strings(VTAG_KEYWORD, "ipp-versions-supported", ["1.0", "1.1", "2.0"])

        # Operations (1setOf enum)
        ops = [OP_PRINT_JOB, OP_VALIDATE_JOB, OP_CREATE_JOB, OP_SEND_DOCUMENT,
               OP_CANCEL_JOB, OP_GET_JOB_ATTRIBUTES, OP_GET_JOBS,
               OP_GET_PRINTER_ATTRIBUTES]
        resp.add_enum("operations-supported", ops[0])
        for op in ops[1:]:
            resp.add_additional_value(VTAG_ENUM, struct.pack(">i", op))

        # Kind
        resp.add_strings(VTAG_KEYWORD, "printer-kind", ["document", "envelope", "photo"])

        # IPP features
        resp.add_string(VTAG_KEYWORD, "ipp-features-supported", "airprint-1.1")

        # Page rates
        resp.add_integer("pages-per-minute", 4)
        resp.add_integer("pages-per-minute-color", 9)

        # Compression
        resp.add_strings(VTAG_KEYWORD, "compression-supported", ["none", "gzip"])

        # Job creation attributes
        resp.add_strings(VTAG_KEYWORD, "job-creation-attributes-supported", [
            "copies", "finishings", "ipp-attribute-fidelity", "job-name",
            "media", "media-col", "orientation-requested", "output-bin",
            "print-quality", "printer-resolution", "sides",
            "print-color-mode", "print-scaling",
        ])

        # Output
        resp.add_string(VTAG_KEYWORD, "output-bin-default", "face-up")
        resp.add_string(VTAG_KEYWORD, "output-bin-supported", "face-up")

        # Charset
        resp.add_string(VTAG_CHARSET, "charset-configured", "utf-8")
        resp.add_string(VTAG_CHARSET, "charset-supported", "utf-8")
        resp.add_string(VTAG_LANGUAGE, "natural-language-configured", "en")
        resp.add_string(VTAG_LANGUAGE, "generated-natural-language-supported", "en")

        # Multiple docs
        resp.add_boolean("multiple-document-jobs-supported", False)
        resp.add_integer("multiple-operation-time-out", 120)

        # Required by ipp2ppd / macOS auto-detection
        resp.add_string(VTAG_URI, "printer-more-info", f"http://{printer.host}/")
        resp.add_integer("printer-up-time", 60)

        # media-col-default (collection) - required for PPD generation
        # begCollection has 0-length value, members are appended raw
        resp.add_attribute(VTAG_COLLECTION, "media-col-default", b"")
        media_col_members = self._build_media_col_bytes(21000, 29700, 300, "stationery")
        resp._current_group.append(media_col_members)

        self._send_ipp_response(resp.build())
        log.info("Sent printer attributes response")

    @staticmethod
    def _build_media_col_bytes(width: int, height: int, margin: int, media_type: str) -> bytes:
        """Build raw bytes for a media-col collection's member attributes.

        IPP collections are encoded as:
          begCollection (0x34) with name and 0-length value
          then member attributes, each preceded by memberAttrName (0x4A)
          endCollection (0x37) with 0-length name and value

        The add_attribute call for the top-level collection handles the
        begCollection tag. We return the inner members + endCollection.
        """
        data = b""

        # media-size (nested collection)
        # memberAttrName
        data += struct.pack(">bh", 0x4A, 0) + struct.pack(">h", 10) + b"media-size"
        # begCollection (nested)
        data += struct.pack(">bh", 0x34, 0) + struct.pack(">h", 0)
        # x-dimension
        data += struct.pack(">bh", 0x4A, 0) + struct.pack(">h", 11) + b"x-dimension"
        data += struct.pack(">bh", 0x21, 0) + struct.pack(">h", 4) + struct.pack(">i", width)
        # y-dimension
        data += struct.pack(">bh", 0x4A, 0) + struct.pack(">h", 11) + b"y-dimension"
        data += struct.pack(">bh", 0x21, 0) + struct.pack(">h", 4) + struct.pack(">i", height)
        # endCollection (nested)
        data += struct.pack(">bh", 0x37, 0) + struct.pack(">h", 0)

        # margins
        for margin_name in [b"media-top-margin", b"media-bottom-margin",
                           b"media-left-margin", b"media-right-margin"]:
            data += struct.pack(">bh", 0x4A, 0) + struct.pack(">h", len(margin_name)) + margin_name
            data += struct.pack(">bh", 0x21, 0) + struct.pack(">h", 4) + struct.pack(">i", margin)

        # media-type
        mt = media_type.encode()
        data += struct.pack(">bh", 0x4A, 0) + struct.pack(">h", 10) + b"media-type"
        data += struct.pack(">bh", 0x44, 0) + struct.pack(">h", len(mt)) + mt

        # endCollection (top-level)
        data += struct.pack(">bh", 0x37, 0) + struct.pack(">h", 0)

        return data

    def _handle_validate_job(self, req):
        resp = IPPResponseBuilder(req.request_id, STATUS_OK)
        resp.start_group(TAG_OPERATION)
        resp.add_string(VTAG_CHARSET, "attributes-charset", "utf-8")
        resp.add_string(VTAG_LANGUAGE, "attributes-natural-language", "en")
        self._send_ipp_response(resp.build())

    def _handle_print_job(self, req):
        """Handle Print-Job: receive document and forward in one step."""
        state = self.server.state
        job_id = state.next_job_id()
        doc_format = req.get_attr_str("document-format") or "application/octet-stream"

        log.info("Print-Job #%d, format: %s, data: %d bytes", job_id, doc_format, len(req.data))

        # Convert and forward
        try:
            self._forward_job(req.data, doc_format, job_id)
        except Exception:
            log.exception("Failed to forward print job #%d", job_id)
            self._send_ipp_error(STATUS_SERVER_ERROR, req.request_id)
            return

        resp = IPPResponseBuilder(req.request_id, STATUS_OK)
        resp.start_group(TAG_OPERATION)
        resp.add_string(VTAG_CHARSET, "attributes-charset", "utf-8")
        resp.add_string(VTAG_LANGUAGE, "attributes-natural-language", "en")
        resp.start_group(TAG_JOB)
        resp.add_integer("job-id", job_id)
        resp.add_string(VTAG_URI, "job-uri", f"{state.proxy_uri}/jobs/{job_id}")
        resp.add_enum("job-state", 9)  # completed
        self._send_ipp_response(resp.build())

    def _handle_create_job(self, req):
        """Handle Create-Job: allocate job ID, wait for Send-Document."""
        state = self.server.state
        job_id = state.next_job_id()
        state.pending_jobs[job_id] = {"format": None}

        log.info("Create-Job #%d", job_id)

        resp = IPPResponseBuilder(req.request_id, STATUS_OK)
        resp.start_group(TAG_OPERATION)
        resp.add_string(VTAG_CHARSET, "attributes-charset", "utf-8")
        resp.add_string(VTAG_LANGUAGE, "attributes-natural-language", "en")
        resp.start_group(TAG_JOB)
        resp.add_integer("job-id", job_id)
        resp.add_string(VTAG_URI, "job-uri", f"{state.proxy_uri}/jobs/{job_id}")
        resp.add_enum("job-state", 3)  # pending
        self._send_ipp_response(resp.build())

    def _handle_send_document(self, req):
        """Handle Send-Document: receive document data for a pending job."""
        state = self.server.state

        # Get job-id from attributes
        job_id_values = req.attributes.get("job-id", [])
        if not job_id_values:
            self._send_ipp_error(STATUS_CLIENT_BAD_REQUEST, req.request_id)
            return
        job_id = struct.unpack(">i", job_id_values[0][1])[0]

        doc_format = req.get_attr_str("document-format") or "application/octet-stream"
        log.info("Send-Document for job #%d, format: %s, data: %d bytes",
                 job_id, doc_format, len(req.data))

        try:
            self._forward_job(req.data, doc_format, job_id)
        except Exception:
            log.exception("Failed to forward document for job #%d", job_id)
            self._send_ipp_error(STATUS_SERVER_ERROR, req.request_id)
            return

        state.pending_jobs.pop(job_id, None)

        resp = IPPResponseBuilder(req.request_id, STATUS_OK)
        resp.start_group(TAG_OPERATION)
        resp.add_string(VTAG_CHARSET, "attributes-charset", "utf-8")
        resp.add_string(VTAG_LANGUAGE, "attributes-natural-language", "en")
        resp.start_group(TAG_JOB)
        resp.add_integer("job-id", job_id)
        resp.add_string(VTAG_URI, "job-uri", f"{state.proxy_uri}/jobs/{job_id}")
        resp.add_enum("job-state", 9)  # completed
        self._send_ipp_response(resp.build())

    def _handle_get_jobs(self, req):
        resp = IPPResponseBuilder(req.request_id, STATUS_OK)
        resp.start_group(TAG_OPERATION)
        resp.add_string(VTAG_CHARSET, "attributes-charset", "utf-8")
        resp.add_string(VTAG_LANGUAGE, "attributes-natural-language", "en")
        self._send_ipp_response(resp.build())

    def _handle_get_job_attributes(self, req):
        state = self.server.state
        job_id_values = req.attributes.get("job-id", [])
        job_id = struct.unpack(">i", job_id_values[0][1])[0] if job_id_values else 0

        resp = IPPResponseBuilder(req.request_id, STATUS_OK)
        resp.start_group(TAG_OPERATION)
        resp.add_string(VTAG_CHARSET, "attributes-charset", "utf-8")
        resp.add_string(VTAG_LANGUAGE, "attributes-natural-language", "en")
        resp.start_group(TAG_JOB)
        resp.add_integer("job-id", job_id)
        resp.add_string(VTAG_URI, "job-uri", f"{state.proxy_uri}/jobs/{job_id}")
        resp.add_enum("job-state", 9)  # completed
        resp.add_string(VTAG_KEYWORD, "job-state-reasons", "job-completed-successfully")
        self._send_ipp_response(resp.build())

    def _handle_cancel_job(self, req):
        resp = IPPResponseBuilder(req.request_id, STATUS_OK)
        resp.start_group(TAG_OPERATION)
        resp.add_string(VTAG_CHARSET, "attributes-charset", "utf-8")
        resp.add_string(VTAG_LANGUAGE, "attributes-natural-language", "en")
        self._send_ipp_response(resp.build())

    def _forward_job(self, data: bytes, content_type: str, job_id: int):
        """Convert document to PWG Raster and forward to the real printer."""
        state = self.server.state
        printer = state.printer

        resolution = printer.pwg_raster_resolutions[0] if printer.pwg_raster_resolutions else 360

        # Convert to PWG Raster
        pwg_data = convert_to_pwg_raster(data, content_type,
                                          resolution=resolution,
                                          color=printer.color)

        log.info("Converted %d bytes (%s) → %d bytes (PWG Raster), forwarding to %s",
                 len(data), content_type, len(pwg_data), printer.host)
        # Debug: save both input and output for inspection
        import tempfile, os
        with open(f"/tmp/debug_input_{job_id}.pdf", "wb") as f:
            f.write(data)
        with open(f"/tmp/debug_output_{job_id}.pwg", "wb") as f:
            f.write(pwg_data)
        log.debug("Saved debug files: /tmp/debug_input_%d.pdf and /tmp/debug_output_%d.pwg", job_id, job_id)

        # Build IPP Print-Job request for the real printer
        ipp = b""
        ipp += struct.pack(">bbhi", 2, 0, OP_PRINT_JOB, job_id)

        # Operation attributes
        ipp += bytes([0x01])  # operation-attributes-tag

        # charset
        ipp += struct.pack(">bh", 0x47, 18) + b"attributes-charset"
        ipp += struct.pack(">h", 5) + b"utf-8"

        # language
        ipp += struct.pack(">bh", 0x48, 27) + b"attributes-natural-language"
        ipp += struct.pack(">h", 2) + b"en"

        # printer-uri
        uri = printer.ipp_uri.encode("utf-8")
        ipp += struct.pack(">bh", 0x45, 11) + b"printer-uri"
        ipp += struct.pack(">h", len(uri)) + uri

        # document-format
        fmt = b"image/pwg-raster"
        ipp += struct.pack(">bh", 0x49, 15) + b"document-format"
        ipp += struct.pack(">h", len(fmt)) + fmt

        # job-name
        name = f"AirPrint-Proxy-Job-{job_id}".encode("utf-8")
        ipp += struct.pack(">bh", 0x42, 8) + b"job-name"
        ipp += struct.pack(">h", len(name)) + name

        # End of attributes
        ipp += bytes([0x03])

        # Append the document data
        payload = ipp + pwg_data

        conn = http.client.HTTPConnection(printer.host, printer.port, timeout=120)
        conn.request("POST", printer.resource, body=payload, headers={
            "Content-Type": "application/ipp",
            "Accept": "application/ipp",
            "Content-Length": str(len(payload)),
        })

        resp = conn.getresponse()
        resp_data = resp.read()
        conn.close()

        if resp.status != 200:
            raise RuntimeError(f"Printer returned HTTP {resp.status}")

        # Check IPP status
        if len(resp_data) >= 4:
            ipp_status = struct.unpack(">h", resp_data[2:4])[0]
            if ipp_status >= 0x0400:
                raise RuntimeError(f"Printer returned IPP error 0x{ipp_status:04x}")

        log.info("Job #%d forwarded successfully", job_id)


class IPPProxyServer(HTTPServer):
    allow_reuse_address = True

    def __init__(self, state: ProxyState):
        self.state = state
        super().__init__(("", state.proxy_port), IPPRequestHandler)


def run_proxy(printer: PrinterConfig, proxy_host: str, proxy_port: int = 8631) -> IPPProxyServer:
    """Create and return the IPP proxy server (call serve_forever() to start)."""
    state = ProxyState(printer, proxy_host, proxy_port)
    return IPPProxyServer(state)
