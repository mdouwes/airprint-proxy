"""IPP protocol helpers for parsing requests and building responses."""

import struct
from dataclasses import dataclass, field


# IPP operation codes
OP_PRINT_JOB = 0x0002
OP_VALIDATE_JOB = 0x0004
OP_CREATE_JOB = 0x0005
OP_SEND_DOCUMENT = 0x0006
OP_CANCEL_JOB = 0x0008
OP_GET_JOB_ATTRIBUTES = 0x0009
OP_GET_JOBS = 0x000A
OP_GET_PRINTER_ATTRIBUTES = 0x000B

# IPP status codes
STATUS_OK = 0x0000
STATUS_OK_IGNORED = 0x0001
STATUS_CLIENT_BAD_REQUEST = 0x0400
STATUS_CLIENT_NOT_FOUND = 0x0406
STATUS_CLIENT_DOCUMENT_FORMAT = 0x040A
STATUS_SERVER_ERROR = 0x0500
STATUS_SERVER_NOT_ACCEPTING = 0x0508

# IPP attribute tags
TAG_OPERATION = 0x01
TAG_JOB = 0x02
TAG_END = 0x03
TAG_PRINTER = 0x04
TAG_UNSUPPORTED = 0x05

# Value tags
VTAG_INTEGER = 0x21
VTAG_BOOLEAN = 0x22
VTAG_ENUM = 0x23
VTAG_OCTET_STRING = 0x30
VTAG_DATETIME = 0x31
VTAG_RESOLUTION = 0x32
VTAG_RANGE = 0x33
VTAG_COLLECTION = 0x34
VTAG_TEXT = 0x41
VTAG_NAME = 0x42
VTAG_KEYWORD = 0x44
VTAG_URI = 0x45
VTAG_CHARSET = 0x47
VTAG_LANGUAGE = 0x48
VTAG_MIME = 0x49


@dataclass
class IPPRequest:
    """Parsed IPP request."""
    version_major: int = 2
    version_minor: int = 0
    operation: int = 0
    request_id: int = 0
    attributes: dict[str, list[tuple[int, bytes]]] = field(default_factory=dict)
    data: bytes = b""

    def get_attr_str(self, name: str) -> str | None:
        values = self.attributes.get(name, [])
        if values:
            try:
                return values[0][1].decode("utf-8")
            except UnicodeDecodeError:
                return None
        return None


def parse_ipp_request(raw: bytes) -> IPPRequest:
    """Parse raw bytes into an IPPRequest."""
    if len(raw) < 8:
        raise ValueError("IPP request too short")

    req = IPPRequest()
    req.version_major = raw[0]
    req.version_minor = raw[1]
    req.operation = struct.unpack(">h", raw[2:4])[0]
    req.request_id = struct.unpack(">i", raw[4:8])[0]

    pos = 8
    current_attr_name = None

    while pos < len(raw):
        tag = raw[pos]
        pos += 1

        # Group tags
        if tag <= 0x05:
            if tag == TAG_END:
                req.data = raw[pos:]
                break
            current_attr_name = None
            continue

        # Value tag
        if pos + 2 > len(raw):
            break
        name_len = struct.unpack(">h", raw[pos:pos + 2])[0]
        pos += 2

        if name_len > 0:
            if pos + name_len > len(raw):
                break
            current_attr_name = raw[pos:pos + name_len].decode("utf-8", errors="replace")
            pos += name_len
        # name_len == 0 means additional value for previous attribute

        if pos + 2 > len(raw):
            break
        val_len = struct.unpack(">h", raw[pos:pos + 2])[0]
        pos += 2

        if pos + val_len > len(raw):
            break
        val_data = raw[pos:pos + val_len]
        pos += val_len

        if current_attr_name:
            if current_attr_name not in req.attributes:
                req.attributes[current_attr_name] = []
            req.attributes[current_attr_name].append((tag, val_data))

    return req


class IPPResponseBuilder:
    """Builds an IPP response."""

    def __init__(self, request_id: int, status: int = STATUS_OK,
                 version_major: int = 2, version_minor: int = 0):
        self.header = struct.pack(">bbhi", version_major, version_minor,
                                  status, request_id)
        self.groups: list[bytes] = []
        self._current_group: list[bytes] = []
        self._current_group_tag: int | None = None

    def start_group(self, group_tag: int):
        self._flush_group()
        self._current_group_tag = group_tag

    def _flush_group(self):
        if self._current_group_tag is not None and self._current_group:
            data = bytes([self._current_group_tag]) + b"".join(self._current_group)
            self.groups.append(data)
        self._current_group = []

    def add_attribute(self, value_tag: int, name: str, value: bytes):
        name_bytes = name.encode("utf-8")
        attr = struct.pack(">bh", value_tag, len(name_bytes))
        attr += name_bytes
        attr += struct.pack(">h", len(value)) + value
        self._current_group.append(attr)

    def add_additional_value(self, value_tag: int, value: bytes):
        """Add another value to the previous attribute (1setOf)."""
        attr = struct.pack(">bh", value_tag, 0)  # name_len=0
        attr += struct.pack(">h", len(value)) + value
        self._current_group.append(attr)

    def add_string(self, value_tag: int, name: str, value: str):
        self.add_attribute(value_tag, name, value.encode("utf-8"))

    def add_strings(self, value_tag: int, name: str, values: list[str]):
        if not values:
            return
        self.add_string(value_tag, name, values[0])
        for v in values[1:]:
            self.add_additional_value(value_tag, v.encode("utf-8"))

    def add_integer(self, name: str, value: int):
        self.add_attribute(VTAG_INTEGER, name, struct.pack(">i", value))

    def add_boolean(self, name: str, value: bool):
        self.add_attribute(VTAG_BOOLEAN, name, bytes([1 if value else 0]))

    def add_enum(self, name: str, value: int):
        self.add_attribute(VTAG_ENUM, name, struct.pack(">i", value))

    def add_resolution(self, name: str, xres: int, yres: int, units: int = 3):
        self.add_attribute(VTAG_RESOLUTION, name,
                           struct.pack(">iiB", xres, yres, units))

    def build(self) -> bytes:
        self._flush_group()
        return self.header + b"".join(self.groups) + bytes([TAG_END])
