"""Portal Typed RPC projection for the local engine's fixed schema."""
from pathlib import Path
import json
import struct

SCHEMA = json.loads(Path(__file__).with_name("local_wire.json").read_bytes())
RECORDS = SCHEMA["records"]
METHODS = SCHEMA["methods"]
FRAME_LIMIT = 1024 * 1024
_HEADER = struct.Struct("<4sHHQIIQ")
_PRIMITIVES = {"u64": ("<Q", 4), "i32": ("<i", 7), "boolean": ("<B", 9)}
_SEED = 14695981039346656037


def _mix(h, value):
    for byte in value.to_bytes(8, "little"):
        h = ((h ^ byte) * 1099511628211) & ((1 << 64) - 1)
    return h


def _fingerprint(codec):
    if codec in RECORDS:
        ident, fields = RECORDS[codec]
        h = _mix(_mix(_mix(_SEED, 30), ident), 1)
        for i, field in enumerate(fields.values(), 1):
            h = _mix(h, _mix(_fingerprint(field), i))
        return h
    if codec.startswith("blob"):
        return _mix(_mix(_SEED, 20), int(codec[4:]))
    return _mix(_SEED, _PRIMITIVES[codec][1])


def _method_hash(result, args):
    h = _mix(_mix(_SEED, _fingerprint(result)), len(args))
    for arg in args:
        h = _mix(h, _fingerprint(arg))
    return h


_METHOD_HEADERS = {
    name: (ident, result, args, _method_hash(result, args))
    for name, (ident, result, args) in METHODS.items()
}


def _encode(codec, value):
    if codec in RECORDS:
        return b"".join(_encode(kind, value.get(name, b"" if kind.startswith("blob") else
                    False if kind == "boolean" else 0)) for name, kind in RECORDS[codec][1].items())
    if codec.startswith("blob"):
        data = value.encode("utf-8", errors="surrogateescape") if isinstance(value, str) else value
        limit = int(codec[4:])
        if not isinstance(data, bytes):
            raise ValueError("local engine expected text or bytes")
        if len(data) > limit:
            raise ValueError(f"local engine payload field exceeds {limit} bytes")
        return struct.pack("<I", len(data)) + data
    if type(value) is not (bool if codec == "boolean" else int):
        raise ValueError(f"local engine expected {codec}")
    try:
        return struct.pack(_PRIMITIVES[codec][0], value)
    except struct.error as exc:
        raise ValueError(f"local engine {codec} out of range") from exc


def decode(codec, data):
    offset = 0

    def read(size):
        nonlocal offset
        end = offset + size
        if end > len(data):
            raise ValueError("short local engine payload")
        result = data[offset:end]
        offset = end
        return result

    def field(kind):
        if kind in RECORDS:
            return {key: field(value) for key, value in RECORDS[kind][1].items()}
        if kind.startswith("blob"):
            size = struct.unpack("<I", read(4))[0]
            if size > int(kind[4:]):
                raise ValueError("invalid local engine blob size")
            return read(size)
        fmt = _PRIMITIVES[kind][0]
        value = struct.unpack(fmt, read(struct.calcsize(fmt)))[0]
        if kind == "boolean":
            if value not in (0, 1):
                raise ValueError("invalid local engine boolean")
            return bool(value)
        return value

    result = field(codec)
    if len(data) != offset:
        raise ValueError("trailing local engine payload")
    return result


def request(name, identifier, *values):
    ident, _, args, signature = _METHOD_HEADERS[name]
    if len(args) != len(values):
        raise ValueError("incorrect local engine method arity")
    body = b"".join(_encode(kind, value) for kind, value in zip(args, values))
    payload = _HEADER.pack(b"PTR1", 1, 0, ident, 1, len(body), signature) + body
    if len(payload) + 8 > FRAME_LIMIT:
        raise ValueError("local engine frame exceeds its wire contract")
    return struct.pack("!IQ", len(payload) + 8, identifier) + payload


def response(name, payload):
    ident, result, _, signature = _METHOD_HEADERS[name]
    magic, version, status, method, revision, size, actual_hash = _HEADER.unpack_from(payload)
    if ((magic, version, method, revision, actual_hash) != (b"PTS1", 1, ident, 1, signature)
            or size != len(payload) - _HEADER.size or status):
        raise ValueError("local engine response rejected by Typed RPC")
    return decode(result, payload[_HEADER.size:])


def accepts_frame(closed, request, session, output, first, end, known_request):
    """An active socket can deliver only current IDs and known request replies."""
    return (
        not closed
        and (not request or known_request)
        and (not session or first <= session < end)
        and (not output or first <= output < end)
    )
