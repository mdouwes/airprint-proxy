"""CLI entry point for the AirPrint proxy."""

import argparse
import logging
import signal
import socket
import sys

from .advertiser import AirPrintAdvertiser
from .config import PrinterConfig, discover_printer
from .proxy import run_proxy

log = logging.getLogger("airprint_proxy")


def get_local_ip() -> str:
    """Get the local IP address used for outgoing connections."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def main():
    parser = argparse.ArgumentParser(
        description="AirPrint proxy for printers that only support PWG Raster",
    )
    parser.add_argument("printer_host", help="IP address or hostname of the target printer")
    parser.add_argument("--printer-port", type=int, default=631,
                        help="IPP port on the target printer (default: 631)")
    parser.add_argument("--printer-resource", default="/ipp/print",
                        help="IPP resource path (default: /ipp/print)")
    parser.add_argument("--proxy-port", type=int, default=8631,
                        help="Port to run the proxy on (default: 8631)")
    parser.add_argument("--proxy-ip", default=None,
                        help="IP to advertise (default: auto-detect)")
    parser.add_argument("--name", default=None,
                        help="Override printer name for advertisement")
    parser.add_argument("--no-discover", action="store_true",
                        help="Skip IPP auto-discovery, use defaults")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Enable debug logging")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    proxy_ip = args.proxy_ip or get_local_ip()
    log.info("Proxy IP: %s", proxy_ip)

    # Discover printer capabilities
    if args.no_discover:
        printer = PrinterConfig(
            name=args.name or "AirPrint Printer",
            host=args.printer_host,
            port=args.printer_port,
            resource=args.printer_resource,
        )
    else:
        log.info("Querying printer at %s:%d%s ...",
                 args.printer_host, args.printer_port, args.printer_resource)
        try:
            printer = discover_printer(args.printer_host, args.printer_port,
                                       args.printer_resource)
        except Exception as e:
            log.error("Failed to query printer: %s", e)
            log.info("Use --no-discover to skip auto-detection")
            sys.exit(1)

    if args.name:
        printer.name = args.name

    log.info("Printer: %s", printer.name)
    log.info("  Make/Model: %s", printer.make_and_model)
    log.info("  UUID: %s", printer.uuid)
    log.info("  Formats: %s", ", ".join(printer.formats))
    log.info("  PWG types: %s", ", ".join(printer.pwg_raster_types))
    log.info("  Color: %s, Duplex: %s", printer.color, printer.duplex)

    if not printer.supports_pwg_raster:
        log.warning("Printer does not report PWG Raster support!")
        log.warning("Forwarded jobs may fail.")

    # Start IPP proxy server
    server = run_proxy(printer, proxy_ip, args.proxy_port)
    log.info("IPP proxy listening on port %d", args.proxy_port)

    # Start mDNS advertisement
    advertiser = AirPrintAdvertiser(printer, proxy_ip, args.proxy_port)
    advertiser.start()
    log.info("AirPrint advertised as '%s'", printer.name)
    log.info("Proxy: ipp://%s:%d/ipp/print → ipp://%s:%d%s",
             proxy_ip, args.proxy_port, printer.host, printer.port, printer.resource)

    # Graceful shutdown
    def shutdown(sig, frame):
        log.info("Shutting down...")
        advertiser.stop()
        server.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    server.serve_forever()


if __name__ == "__main__":
    main()
