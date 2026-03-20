"""Convert incoming print data (URF, PDF) to PWG Raster for the target printer."""

import logging
import shutil
import subprocess
import tempfile
import os
from pathlib import Path

log = logging.getLogger(__name__)


def _find_tool(name: str) -> str | None:
    return shutil.which(name)


def urf_to_pwg_raster(data: bytes, resolution: int = 360) -> bytes:
    """Convert Apple URF (Unified Raster Format) to PWG Raster.

    URF and PWG Raster are structurally similar. URF is essentially Apple's
    variant of CUPS Raster/PWG Raster. We convert via rastertopwg if available,
    or do a direct header rewrite.
    """
    # URF files start with "UNIRAST\0"
    if not data.startswith(b"UNIRAST\x00"):
        raise ValueError("Not a valid URF file (missing UNIRAST header)")

    # Try using cups filters if available
    rastertopwg = _find_tool("rastertopwg")
    if rastertopwg:
        return _run_filter(rastertopwg, data, resolution=resolution)

    # Fallback: convert URF to PWG Raster manually
    # URF and PWG Raster have similar page structures. The main difference is
    # the file/page headers. We need to rewrite the headers.
    return _manual_urf_to_pwg(data, resolution)


def _manual_urf_to_pwg(data: bytes, resolution: int) -> bytes:
    """Convert URF to PWG Raster by rewriting headers.

    URF format:
      File header: "UNIRAST\0" + uint32_be(num_pages)
      Per page: 32-byte page header + compressed raster data

    PWG Raster format:
      File header: "RaS2" (PWG Raster magic)
      Per page: 1796-byte page header + compressed raster data

    Both use the same PackBits compression for raster data.
    """
    import struct

    if len(data) < 12:
        raise ValueError("URF data too short")

    num_pages = struct.unpack(">I", data[8:12])[0]
    pos = 12  # past UNIRAST\0 + page count

    output = b"RaS2"  # PWG Raster sync word

    for page_num in range(num_pages):
        if pos + 32 > len(data):
            raise ValueError(f"URF data truncated at page {page_num}")

        # Parse URF page header (32 bytes)
        urf_header = data[pos:pos + 32]
        pos += 32

        bpp = urf_header[0]  # bits per pixel (1=gray, 3=rgb, 4=cmyk)
        colorspace = urf_header[1]  # 0=gray, 1=rgb
        duplex = urf_header[2]
        quality = urf_header[3]
        # Bytes 4-7: reserved/media type/input slot
        width = struct.unpack(">I", urf_header[8:12])[0]
        height = struct.unpack(">I", urf_header[12:16])[0]
        urf_res = struct.unpack(">I", urf_header[16:20])[0]
        # Bytes 20-31: reserved

        if urf_res == 0:
            urf_res = resolution

        # Build PWG Raster page header (cups_page_header2_t, 1796 bytes, big-endian)
        pwg_header = bytearray(1796)

        # MediaClass at offset 0 (64 bytes)
        pwg_header[0:10] = b"PwgRaster\x00"

        # HWResolution[2] at offset 276
        struct.pack_into(">II", pwg_header, 276, urf_res, urf_res)

        # NumCopies at offset 340
        struct.pack_into(">I", pwg_header, 340, 1)

        # cupsWidth(372), cupsHeight(376)
        struct.pack_into(">II", pwg_header, 372, width, height)

        bits_per_pixel = 8 if colorspace == 0 else 24
        bytes_per_line = width * (bits_per_pixel // 8)

        # cupsMediaType(380)=0, cupsBitsPerColor(384), cupsBitsPerPixel(388),
        # cupsBytesPerLine(392), cupsColorOrder(396)=chunky, cupsColorSpace(400)
        struct.pack_into(">I", pwg_header, 384, 8)
        struct.pack_into(">I", pwg_header, 388, bits_per_pixel)
        struct.pack_into(">I", pwg_header, 392, bytes_per_line)
        struct.pack_into(">I", pwg_header, 396, 0)  # chunky
        pwg_colorspace = 18 if colorspace == 0 else 19
        struct.pack_into(">I", pwg_header, 400, pwg_colorspace)

        # cupsNumColors at offset 420
        num_colors = 1 if colorspace == 0 else 3
        struct.pack_into(">I", pwg_header, 420, num_colors)

        # cupsInteger[0]=TotalPageCount(452), [1]=CrossFeedTransform(456), [2]=FeedTransform(460)
        struct.pack_into(">I", pwg_header, 452, page_num + 1)
        struct.pack_into(">i", pwg_header, 456, 1)
        struct.pack_into(">i", pwg_header, 460, 1)

        output += bytes(pwg_header)

        # Copy compressed raster data as-is (both use PackBits)
        # We need to figure out where this page's data ends.
        # Each line is: repeat_count(1 byte) + packbits_line_data
        # We need to read `height` lines of data
        for _line in range(height):
            if pos >= len(data):
                raise ValueError(f"URF data truncated in raster at page {page_num}")

            # PackBits line: first byte is repeat count (1-256, stored as 0-255)
            line_repeat = data[pos] + 1
            pos += 1

            # Then packbits-compressed line data
            remaining = bytes_per_line
            line_data_start = pos
            while remaining > 0:
                if pos >= len(data):
                    raise ValueError("URF data truncated in packbits")
                cmd = data[pos]
                pos += 1
                if cmd < 128:
                    # Literal run: cmd+1 bytes follow
                    count = cmd + 1
                    pos += count
                    remaining -= count
                elif cmd > 128:
                    # Repeated byte: 257-cmd copies of next byte
                    count = 257 - cmd
                    pos += 1
                    remaining -= count
                else:
                    # cmd == 128: no-op (padding)
                    pass

            line_data = data[line_data_start:pos]

            # Write the same format for PWG Raster
            output += bytes([line_repeat - 1]) + line_data

    return output


def pdf_to_pwg_raster(data: bytes, resolution: int = 360,
                       width_pts: int = 595, height_pts: int = 842,
                       color: bool = True) -> bytes:
    """Convert PDF to PWG Raster using Ghostscript."""
    gs = _find_tool("gs")
    if not gs:
        raise RuntimeError("Ghostscript (gs) is required for PDF conversion but not found. "
                          "Install it with: apt install ghostscript")

    with tempfile.TemporaryDirectory() as tmpdir:
        pdf_path = os.path.join(tmpdir, "input.pdf")
        ppm_path = os.path.join(tmpdir, "page")

        with open(pdf_path, "wb") as f:
            f.write(data)

        # Render PDF to raw RGB/Gray PPM images
        device = "ppmraw" if color else "pgmraw"
        width_px = int(width_pts * resolution / 72)
        height_px = int(height_pts * resolution / 72)

        cmd = [
            gs, "-q", "-dNOPAUSE", "-dBATCH", "-dSAFER",
            f"-sDEVICE={device}",
            f"-r{resolution}",
            f"-g{width_px}x{height_px}",
            "-dFitPage",
            f"-sOutputFile={ppm_path}-%03d.ppm",
            pdf_path,
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=120)
        if result.returncode != 0:
            raise RuntimeError(f"Ghostscript failed: {result.stderr.decode(errors='replace')}")

        # Convert PPM pages to PWG Raster
        pages = sorted(Path(tmpdir).glob("page-*.ppm"))
        if not pages:
            raise RuntimeError("Ghostscript produced no output pages")

        return _ppm_pages_to_pwg(pages, resolution, color)


def _packbits_encode_line(buf, line: bytes):
    """Encode a single raster line using PackBits compression.

    PackBits format:
      0-127: literal run of N+1 bytes follow
      129-255 (i.e. -1 to -127 as signed): repeat next byte 257-N times
      128: no-op
    """
    i = 0
    n = len(line)
    while i < n:
        # Look for a run of identical bytes
        run_start = i
        while i + 1 < n and line[i] == line[i + 1] and i - run_start < 127:
            i += 1

        run_len = i - run_start + 1
        if run_len >= 3:
            # Emit repeat: (257 - run_len) as unsigned byte, then the byte
            buf.write(bytes([257 - run_len, line[run_start]]))
            i = run_start + run_len
        else:
            # Collect literal bytes (non-repeating)
            i = run_start
            lit_start = i
            while i < n:
                # Check if next 3+ bytes are a run — if so, stop literal here
                if i + 2 < n and line[i] == line[i + 1] == line[i + 2]:
                    break
                i += 1
                if i - lit_start >= 128:
                    break
            lit_len = i - lit_start
            buf.write(bytes([lit_len - 1]))
            buf.write(line[lit_start:lit_start + lit_len])


def _ppm_pages_to_pwg(pages: list[Path], resolution: int, color: bool) -> bytes:
    """Convert a list of PPM/PGM files to PWG Raster format."""
    import struct
    import io

    buf = io.BytesIO()
    buf.write(b"RaS2")

    for page_num, page_path in enumerate(pages):
        raw = page_path.read_bytes()
        # Parse PPM/PGM header
        header_lines = []
        pos = 0
        while len(header_lines) < 3:
            end = raw.index(b"\n", pos)
            line = raw[pos:end].strip()
            pos = end + 1
            if not line.startswith(b"#"):
                header_lines.append(line)

        magic = header_lines[0]
        w, h = [int(x) for x in header_lines[1].split()]
        pixel_data = memoryview(raw)[pos:]

        is_color = magic == b"P6"
        bpp = 3 if is_color else 1
        bits_per_pixel = bpp * 8
        bytes_per_line = w * bpp

        # Build PWG page header (cups_page_header2_t, 1796 bytes)
        pwg_header = bytearray(1796)
        pwg_header[0:10] = b"PwgRaster\x00"  # MediaClass
        struct.pack_into(">II", pwg_header, 276, resolution, resolution)  # HWResolution
        struct.pack_into(">I", pwg_header, 340, 1)  # NumCopies
        struct.pack_into(">II", pwg_header, 372, w, h)  # cupsWidth, cupsHeight
        # cupsMediaType(380)=0, cupsBitsPerColor(384), cupsBitsPerPixel(388),
        # cupsBytesPerLine(392), cupsColorOrder(396), cupsColorSpace(400)
        struct.pack_into(">I", pwg_header, 384, 8)
        struct.pack_into(">I", pwg_header, 388, bits_per_pixel)
        struct.pack_into(">I", pwg_header, 392, bytes_per_line)
        struct.pack_into(">I", pwg_header, 396, 0)  # chunky
        struct.pack_into(">I", pwg_header, 400, 19 if is_color else 18)  # sRGB/sGray
        struct.pack_into(">I", pwg_header, 420, 3 if is_color else 1)  # cupsNumColors
        struct.pack_into(">I", pwg_header, 452, page_num + 1)  # TotalPageCount
        struct.pack_into(">i", pwg_header, 456, 1)  # CrossFeedTransform
        struct.pack_into(">i", pwg_header, 460, 1)  # FeedTransform

        buf.write(pwg_header)

        # Encode each line with PackBits compression
        prev_line = None
        for y in range(h):
            line_start = y * bytes_per_line
            line = bytes(pixel_data[line_start:line_start + bytes_per_line])

            # Line repeat: if same as previous, increment repeat count
            if line == prev_line:
                # Seek back to the repeat count byte and increment
                pos = buf.tell()
                buf.seek(repeat_pos)
                repeat_count = buf.read(1)[0]
                if repeat_count < 255:
                    buf.seek(repeat_pos)
                    buf.write(bytes([repeat_count + 1]))
                    buf.seek(pos)
                    continue
                buf.seek(pos)

            prev_line = line

            # Write repeat count = 0 (1 copy)
            repeat_pos = buf.tell()
            buf.write(b"\x00")

            # PackBits compression for the line
            _packbits_encode_line(buf, line)

    return buf.getvalue()


def _run_filter(filter_path: str, data: bytes, resolution: int = 360) -> bytes:
    """Run a CUPS filter on input data."""
    env = os.environ.copy()
    env["CONTENT_TYPE"] = "image/urf"
    env["PRINTER"] = "airprint-proxy"

    result = subprocess.run(
        [filter_path, "1", "anonymous", "untitled", "1",
         f"Resolution={resolution}dpi"],
        input=data, capture_output=True, timeout=120, env=env,
    )
    if result.returncode not in (0, 1):
        raise RuntimeError(f"Filter {filter_path} failed: {result.stderr.decode(errors='replace')}")
    return result.stdout


def convert_to_pwg_raster(data: bytes, content_type: str,
                          resolution: int = 360, color: bool = True) -> bytes:
    """Convert print data to PWG Raster based on content type."""
    ct = content_type.lower().split(";")[0].strip()

    if ct == "image/pwg-raster":
        return data  # Already in the right format

    if ct == "image/urf":
        log.info("Converting URF to PWG Raster")
        return urf_to_pwg_raster(data, resolution=resolution)

    if ct == "application/pdf":
        log.info("Converting PDF to PWG Raster")
        return pdf_to_pwg_raster(data, resolution=resolution, color=color)

    if ct in ("application/octet-stream", ""):
        # Try to detect format from magic bytes
        if data[:8] == b"UNIRAST\x00":
            log.info("Detected URF format, converting to PWG Raster")
            return urf_to_pwg_raster(data, resolution=resolution)
        if data[:5] == b"%PDF-":
            log.info("Detected PDF format, converting to PWG Raster")
            return pdf_to_pwg_raster(data, resolution=resolution, color=color)
        if data[:4] == b"RaS2":
            log.info("Detected PWG Raster format, passing through")
            return data

    raise ValueError(f"Unsupported content type: {ct}")
