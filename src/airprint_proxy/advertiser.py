"""mDNS/Bonjour advertisement for the AirPrint proxy.

On macOS: uses dns-sd (native Bonjour) which supports _universal subtype natively.
On Linux: uses python-zeroconf, which works directly with avahi if present.
"""

import logging
import platform
import shlex
import socket
import subprocess
import threading

from zeroconf import ServiceInfo, Zeroconf

from .config import PrinterConfig

log = logging.getLogger(__name__)


def build_txt_records(printer: PrinterConfig, proxy_port: int) -> dict[str, str]:
    """Build TXT records that make macOS/iOS recognize this as an AirPrint printer."""
    resolution = printer.pwg_raster_resolutions[0] if printer.pwg_raster_resolutions else 360

    urf_string = f"CP1,MT1-2-8,RS{resolution},SRGB24,W8,OB10,PQ3-4-5"
    if not printer.duplex:
        urf_string += ",DM1"
    else:
        urf_string += ",DM1-3"

    return {
        "txtvers": "1",
        "qtotal": "1",
        "rp": "ipp/print",
        "ty": printer.make_and_model or printer.name,
        "adminurl": f"http://{printer.host}/",
        "note": printer.name,
        "priority": "0",
        "product": f"({printer.make_and_model or printer.name})",
        "printer-state": "3",
        "printer-type": "0x809046",
        "pdl": "image/urf,image/pwg-raster,application/pdf",
        "URF": urf_string,
        "UUID": printer.uuid,
        "Color": "T" if printer.color else "F",
        "Duplex": "T" if printer.duplex else "F",
        "Copies": "T",
        "Scan": "F",
        "Fax": "F",
        "kind": "document,envelope,photo",
        "PaperMax": "legal-A4",
    }


class AirPrintAdvertiser:
    """Advertise the proxy as an AirPrint printer via mDNS/Bonjour."""

    def __init__(self, printer: PrinterConfig, proxy_ip: str, proxy_port: int):
        self.printer = printer
        self.proxy_ip = proxy_ip
        self.proxy_port = proxy_port
        self.zeroconf: Zeroconf | None = None
        self.service_info: ServiceInfo | None = None
        self._dns_sd_proc: subprocess.Popen | None = None

    def start(self):
        txt = build_txt_records(self.printer, self.proxy_port)

        if platform.system() == "Darwin":
            self._start_macos(txt)
        else:
            self._start_zeroconf(txt)

        log.info("AirPrint service registered")

    def _start_macos(self, txt: dict[str, str]):
        """Use dns-sd on macOS — supports _universal subtype natively."""
        txt_args = [f"{k}={v}" for k, v in txt.items()]
        # _ipp._tcp,_universal registers both types with one call
        cmd = [
            "dns-sd", "-R", self.printer.name,
            "_ipp._tcp,_universal", "local", str(self.proxy_port),
        ] + txt_args

        log.debug("dns-sd command: %s", shlex.join(cmd))
        self._dns_sd_proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        log.info("Registered AirPrint service via dns-sd (with _universal subtype): %s", self.printer.name)

    def _start_zeroconf(self, txt: dict[str, str]):
        """Use python-zeroconf for Linux."""
        service_name = f"{self.printer.name}._ipp._tcp.local."
        hostname = socket.gethostname()
        if not hostname.endswith(".local"):
            hostname += ".local"
        hostname += "."

        self.service_info = ServiceInfo(
            type_="_ipp._tcp.local.",
            name=service_name,
            addresses=[socket.inet_aton(self.proxy_ip)],
            port=self.proxy_port,
            properties=txt,
            server=hostname,
        )

        self.zeroconf = Zeroconf()
        self.zeroconf.register_service(self.service_info)
        log.info("Registered mDNS service: %s", service_name)

        # Attempt subtype PTR record via low-level DNS injection
        self._inject_universal_subtype(service_name, txt, hostname)

    def _inject_universal_subtype(self, service_name: str, txt: dict, hostname: str):
        """Inject _universal._sub._ipp._tcp PTR record into zeroconf responses."""
        try:
            from zeroconf._dns import DNSPointer
            from zeroconf.const import _CLASS_IN, _TYPE_PTR
            from zeroconf import DNSOutgoing
            from zeroconf.const import _FLAGS_QR_RESPONSE, _FLAGS_AA

            TTL = 4500
            ptr = DNSPointer(
                "_universal._sub._ipp._tcp.local.",
                _TYPE_PTR,
                _CLASS_IN,
                TTL,
                service_name,
            )

            # Periodically re-announce the subtype PTR (since we can't add it to the registry)
            def announce_subtype():
                if self.zeroconf is None:
                    return
                try:
                    out = DNSOutgoing(_FLAGS_QR_RESPONSE | _FLAGS_AA)
                    out.add_answer_at_time(ptr, 0)
                    self.zeroconf.send(out)
                except Exception:
                    pass

            announce_subtype()
            log.info("Sent _universal subtype PTR announcement")

            # Re-announce every 30s to keep it alive in client caches
            self._subtype_timer = threading.Timer(30, self._reannounce_subtype, args=[ptr])
            self._subtype_timer.daemon = True
            self._subtype_timer.start()
        except Exception as e:
            log.warning("Could not inject _universal subtype PTR: %s", e)

    def _reannounce_subtype(self, ptr):
        if self.zeroconf is None:
            return
        try:
            from zeroconf import DNSOutgoing
            from zeroconf.const import _FLAGS_QR_RESPONSE, _FLAGS_AA
            out = DNSOutgoing(_FLAGS_QR_RESPONSE | _FLAGS_AA)
            out.add_answer_at_time(ptr, 0)
            self.zeroconf.send(out)
        except Exception:
            pass
        self._subtype_timer = threading.Timer(30, self._reannounce_subtype, args=[ptr])
        self._subtype_timer.daemon = True
        self._subtype_timer.start()

    def stop(self):
        if self._dns_sd_proc is not None:
            self._dns_sd_proc.terminate()
            self._dns_sd_proc = None
            log.info("dns-sd process stopped")

        if self.zeroconf and self.service_info:
            log.info("Unregistering mDNS services...")
            self.zeroconf.unregister_all_services()
            self.zeroconf.close()
            self.zeroconf = None
