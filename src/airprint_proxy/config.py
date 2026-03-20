"""Printer configuration and capability detection via IPP."""

import struct
import http.client
from dataclasses import dataclass, field


@dataclass
class PrinterConfig:
    """Configuration for the target printer."""
    name: str
    host: str
    port: int = 631
    resource: str = "/ipp/print"
    uuid: str = ""
    # Capabilities discovered via IPP
    formats: list[str] = field(default_factory=list)
    pwg_raster_types: list[str] = field(default_factory=list)
    pwg_raster_resolutions: list[int] = field(default_factory=list)
    color: bool = True
    duplex: bool = False
    make_and_model: str = ""

    @property
    def ipp_uri(self) -> str:
        return f"ipp://{self.host}:{self.port}{self.resource}"

    @property
    def supports_pwg_raster(self) -> bool:
        return "image/pwg-raster" in self.formats


def _build_get_attributes_request(uri: str) -> bytes:
    """Build an IPP Get-Printer-Attributes request."""
    ipp = b""
    ipp += struct.pack(">bbhi", 2, 0, 0x000B, 1)  # IPP 2.0, Get-Printer-Attributes

    # Operation attributes group
    ipp += b"\x01"

    # charset
    ipp += struct.pack(">bh", 0x47, 18) + b"attributes-charset"
    ipp += struct.pack(">h", 5) + b"utf-8"

    # natural language
    ipp += struct.pack(">bh", 0x48, 27) + b"attributes-natural-language"
    ipp += struct.pack(">h", 2) + b"en"

    # printer-uri
    ipp += struct.pack(">bh", 0x45, 11) + b"printer-uri"
    uri_bytes = uri.encode("utf-8")
    ipp += struct.pack(">h", len(uri_bytes)) + uri_bytes

    # requested-attributes = "all"
    ipp += struct.pack(">bh", 0x44, 20) + b"requested-attributes"
    ipp += struct.pack(">h", 3) + b"all"

    # End of attributes
    ipp += b"\x03"
    return ipp


def _parse_ipp_strings(data: bytes, attr_name: str) -> list[str]:
    """Extract string attribute values from raw IPP response bytes.

    This is a simple parser that finds attribute names and extracts
    their string values. It handles 1setOf by collecting consecutive
    values after the attribute name.
    """
    results = []
    name_bytes = attr_name.encode("utf-8")
    pos = 0

    while pos < len(data) - 4:
        # Look for the attribute name
        idx = data.find(name_bytes, pos)
        if idx == -1:
            break

        # The name length is 2 bytes before the name
        if idx < 2:
            pos = idx + len(name_bytes)
            continue

        name_len = struct.unpack(">h", data[idx - 2:idx])[0]
        if name_len != len(name_bytes):
            pos = idx + len(name_bytes)
            continue

        # Value length follows the name
        val_start = idx + len(name_bytes)
        if val_start + 2 > len(data):
            break

        val_len = struct.unpack(">h", data[val_start:val_start + 2])[0]
        val_data = data[val_start + 2:val_start + 2 + val_len]
        try:
            results.append(val_data.decode("utf-8"))
        except UnicodeDecodeError:
            pass

        # Check for additional values (1setOf) - they have name_len=0
        pos = val_start + 2 + val_len
        while pos + 5 < len(data):
            tag = data[pos]
            next_name_len = struct.unpack(">h", data[pos + 1:pos + 3])[0]
            if next_name_len != 0:
                break
            # This is a continuation value
            next_val_len = struct.unpack(">h", data[pos + 3:pos + 5])[0]
            next_val = data[pos + 5:pos + 5 + next_val_len]
            try:
                results.append(next_val.decode("utf-8"))
            except UnicodeDecodeError:
                pass
            pos = pos + 5 + next_val_len

        break

    return results


def _parse_ipp_resolutions(data: bytes, attr_name: str) -> list[int]:
    """Extract resolution values (dpi) from raw IPP response.

    Resolution attributes are 9 bytes: int32(xres) + int32(yres) + byte(units).
    Units: 3 = dpi, 4 = dpcm.
    Returns list of x-resolution values in dpi.
    """
    results = []
    name_bytes = attr_name.encode("utf-8")
    idx = data.find(name_bytes)
    if idx == -1:
        return results

    # Verify name length
    if idx < 2:
        return results
    name_len = struct.unpack(">h", data[idx - 2:idx])[0]
    if name_len != len(name_bytes):
        return results

    val_start = idx + len(name_bytes)
    if val_start + 2 > len(data):
        return results

    val_len = struct.unpack(">h", data[val_start:val_start + 2])[0]
    if val_len == 9:
        val_data = data[val_start + 2:val_start + 2 + 9]
        xres, yres, units = struct.unpack(">iiB", val_data)
        if units == 4:  # dpcm to dpi
            xres = int(xres * 2.54)
        results.append(xres)

    # Check for continuation values
    pos = val_start + 2 + val_len
    while pos + 5 < len(data):
        next_name_len = struct.unpack(">h", data[pos + 1:pos + 3])[0]
        if next_name_len != 0:
            break
        next_val_len = struct.unpack(">h", data[pos + 3:pos + 5])[0]
        if next_val_len == 9:
            val_data = data[pos + 5:pos + 5 + 9]
            xres, yres, units = struct.unpack(">iiB", val_data)
            if units == 4:
                xres = int(xres * 2.54)
            results.append(xres)
        pos = pos + 5 + next_val_len

    return results


def _parse_ipp_bool(data: bytes, attr_name: str) -> bool | None:
    """Extract a boolean attribute from raw IPP response."""
    name_bytes = attr_name.encode("utf-8")
    idx = data.find(name_bytes)
    if idx == -1:
        return None
    val_start = idx + len(name_bytes)
    if val_start + 3 > len(data):
        return None
    val_len = struct.unpack(">h", data[val_start:val_start + 2])[0]
    if val_len == 1:
        return data[val_start + 2] != 0
    return None


def discover_printer(host: str, port: int = 631, resource: str = "/ipp/print") -> PrinterConfig:
    """Query a printer via IPP and return its configuration."""
    uri = f"ipp://{host}:{port}{resource}"
    request = _build_get_attributes_request(uri)

    conn = http.client.HTTPConnection(host, port, timeout=10)
    conn.request("POST", resource, body=request, headers={
        "Content-Type": "application/ipp",
        "Accept": "application/ipp",
    })
    resp = conn.getresponse()
    data = resp.read()
    conn.close()

    if resp.status != 200:
        raise ConnectionError(f"IPP query failed with status {resp.status}")

    config = PrinterConfig(name="", host=host, port=port, resource=resource)

    # Extract attributes
    names = _parse_ipp_strings(data, "printer-dns-sd-name")
    config.name = names[0] if names else "Unknown Printer"

    models = _parse_ipp_strings(data, "printer-make-and-model")
    config.make_and_model = models[0] if models else config.name

    uuids = _parse_ipp_strings(data, "printer-uuid")
    if uuids:
        config.uuid = uuids[0].replace("urn:uuid:", "")

    config.formats = _parse_ipp_strings(data, "document-format-supported")
    config.pwg_raster_types = _parse_ipp_strings(data, "pwg-raster-document-type-supported")
    config.pwg_raster_resolutions = _parse_ipp_resolutions(data, "pwg-raster-document-resolution-supported")

    sides = _parse_ipp_strings(data, "sides-supported")
    config.duplex = any(s != "one-sided" for s in sides)

    color = _parse_ipp_bool(data, "color-supported")
    if color is not None:
        config.color = color

    return config
