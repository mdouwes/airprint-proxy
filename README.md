# airprint-proxy

An IPP proxy that makes PWG-Raster-only printers work with AirPrint.

## The problem

Some network printers (like the Epson ET-2810) have an IPP endpoint but only
support PWG Raster and the vendor's proprietary raster format. They do not
support URF (Apple Raster) or PDF.

Apple's AirPrint on macOS and iOS expects printers to speak URF or PDF. When
it encounters a printer that only speaks PWG Raster, it either refuses to
auto-detect a driver, or sends data in the wrong format. The result: the
printer spits out pages of garbage characters, or doesn't show up at all.

Linux printing works fine with these printers because CUPS properly queries
the printer's IPP attributes and sends PWG Raster. AirPrint does not.

## The solution

This proxy sits between Apple devices and the printer on a Linux host
(or any machine that runs 24/7 on your network):

```
macOS/iOS  --URF/PDF-->  [airprint-proxy]  --PWG Raster-->  Printer
```

It does four things:

1. Queries the real printer via IPP to discover its actual capabilities
2. Advertises itself on the network via mDNS/Bonjour as an AirPrint printer
   that supports URF and PDF (what Apple devices expect)
3. Accepts print jobs from macOS/iOS in whatever format they send
4. Converts URF or PDF to PWG Raster and forwards the job to the real printer

## Requirements

- Python 3.11+
- `ghostscript` for PDF-to-raster conversion: `apt install ghostscript`
- The target printer must support `image/pwg-raster` over IPP

## Install

```bash
pip install .
```

## Usage

```bash
# Auto-discovers printer capabilities and starts the proxy
airprint-proxy 192.168.178.109

# Custom proxy port (default: 8631)
airprint-proxy 192.168.178.109 --proxy-port 9631

# Override the advertised printer name
airprint-proxy 192.168.178.109 --name "My Printer"

# Verbose logging (useful for debugging)
airprint-proxy 192.168.178.109 -v

# Skip IPP auto-discovery, use safe defaults
airprint-proxy 192.168.178.109 --no-discover --name "EPSON ET-2810"
```

On startup, the proxy:

1. Queries the printer at the given IP for its capabilities (formats,
   resolution, color support, paper sizes, UUID, etc.)
2. Starts an IPP server on port 8631
3. Registers the printer via mDNS with the `_ipp._tcp` and
   `_universal._sub._ipp._tcp` service types
4. Waits for print jobs, converts them, and forwards them

## IPP operations

The proxy implements enough of the IPP protocol to satisfy macOS and iOS:

- `Get-Printer-Attributes` -- reports URF, PDF, and PWG Raster support
- `Print-Job` -- single-step print
- `Create-Job` + `Send-Document` -- two-step print (used by macOS)
- `Validate-Job`, `Get-Jobs`, `Get-Job-Attributes`, `Cancel-Job`

## Running as a systemd service

Create `/etc/systemd/system/airprint-proxy.service`:

```ini
[Unit]
Description=AirPrint Proxy for Epson ET-2810
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/local/bin/airprint-proxy 192.168.178.109
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Then:

```bash
systemctl daemon-reload
systemctl enable --now airprint-proxy
```

## Tested with

- Epson ET-2810 Series (PWG Raster only, no URF, no PDF)
- macOS Sequoia
- iOS 18

Should work with any IPP printer that supports `image/pwg-raster` but
not `image/urf`.

## License

MIT
