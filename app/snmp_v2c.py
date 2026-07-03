import random
import socket
from dataclasses import dataclass
from typing import Any


OID = tuple[int, ...]


class SnmpError(RuntimeError):
    pass


@dataclass(frozen=True)
class VarBind:
    oid: OID
    value: Any
    tag: int


class SnmpV2cClient:
    def __init__(self, host: str, community: str, port: int = 161, timeout: float = 2.0, retries: int = 1) -> None:
        self.host = host
        self.community = community
        self.port = port
        self.timeout = timeout
        self.retries = retries

    def walk(self, base_oid: str | OID, max_repetitions: int = 25, max_rows: int = 10000) -> list[VarBind]:
        base = parse_oid(base_oid)
        current = base
        rows: list[VarBind] = []
        while len(rows) < max_rows:
            batch = self.get_bulk([current], max_repetitions=max_repetitions)
            if not batch:
                break
            advanced = False
            for item in batch:
                if not oid_starts_with(item.oid, base):
                    return rows
                rows.append(item)
                current = item.oid
                advanced = True
                if len(rows) >= max_rows:
                    break
            if not advanced:
                break
        return rows

    def get_bulk(self, oids: list[str | OID], non_repeaters: int = 0, max_repetitions: int = 25) -> list[VarBind]:
        request_id = random.randint(1, 2_147_483_000)
        packet = encode_message(
            community=self.community,
            pdu_tag=0xA5,
            request_id=request_id,
            error_status=non_repeaters,
            error_index=max_repetitions,
            oids=[parse_oid(oid) for oid in oids],
        )
        response = self._send(packet)
        return decode_response(response, expected_request_id=request_id)

    def get(self, oids: list[str | OID]) -> list[VarBind]:
        request_id = random.randint(1, 2_147_483_000)
        packet = encode_message(
            community=self.community,
            pdu_tag=0xA0,
            request_id=request_id,
            error_status=0,
            error_index=0,
            oids=[parse_oid(oid) for oid in oids],
        )
        response = self._send(packet)
        return decode_response(response, expected_request_id=request_id)

    def _send(self, packet: bytes) -> bytes:
        last_error: Exception | None = None
        for _ in range(self.retries + 1):
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                    sock.settimeout(self.timeout)
                    sock.sendto(packet, (self.host, self.port))
                    data, _ = sock.recvfrom(65535)
                    return data
            except OSError as exc:
                last_error = exc
        raise SnmpError(f"SNMP request failed for {self.host}:{self.port}: {last_error}")


def encode_message(
    community: str,
    pdu_tag: int,
    request_id: int,
    error_status: int,
    error_index: int,
    oids: list[OID],
) -> bytes:
    varbinds = b"".join(encode_sequence(encode_oid(oid) + encode_null()) for oid in oids)
    pdu = encode_tlv(
        pdu_tag,
        encode_integer(request_id)
        + encode_integer(error_status)
        + encode_integer(error_index)
        + encode_sequence(varbinds),
    )
    return encode_sequence(encode_integer(1) + encode_octet_string(community.encode("utf-8")) + pdu)


def decode_response(packet: bytes, expected_request_id: int | None = None) -> list[VarBind]:
    reader = BerReader(packet)
    outer = reader.read_constructed(0x30)
    version = outer.read_int()
    if version != 1:
        raise SnmpError(f"unsupported SNMP response version: {version}")
    outer.read_octet_string()
    pdu_tag, pdu_body = outer.read_any()
    if pdu_tag != 0xA2:
        raise SnmpError(f"unexpected SNMP PDU tag: 0x{pdu_tag:02x}")
    pdu = BerReader(pdu_body)
    request_id = pdu.read_int()
    if expected_request_id is not None and request_id != expected_request_id:
        raise SnmpError("SNMP response request-id mismatch")
    error_status = pdu.read_int()
    error_index = pdu.read_int()
    if error_status != 0:
        raise SnmpError(f"SNMP error status={error_status} index={error_index}")
    varbind_reader = pdu.read_constructed(0x30)
    rows: list[VarBind] = []
    while not varbind_reader.eof:
        vb = varbind_reader.read_constructed(0x30)
        oid = vb.read_oid()
        tag, value_bytes = vb.read_any()
        rows.append(VarBind(oid=oid, value=decode_value(tag, value_bytes), tag=tag))
    return rows


def decode_value(tag: int, data: bytes) -> Any:
    if tag == 0x02:
        return decode_signed_int(data)
    if tag in (0x41, 0x42, 0x43, 0x46):
        return decode_unsigned_int(data)
    if tag == 0x04:
        return decode_text(data)
    if tag == 0x05:
        return None
    if tag == 0x06:
        return decode_oid(data)
    if tag in (0x80, 0x81, 0x82):
        return None
    return data


class BerReader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    @property
    def eof(self) -> bool:
        return self.pos >= len(self.data)

    def read_any(self) -> tuple[int, bytes]:
        if self.eof:
            raise SnmpError("unexpected end of BER packet")
        tag = self.data[self.pos]
        self.pos += 1
        length = self._read_length()
        end = self.pos + length
        if end > len(self.data):
            raise SnmpError("BER length exceeds packet size")
        value = self.data[self.pos:end]
        self.pos = end
        return tag, value

    def read_constructed(self, expected_tag: int) -> "BerReader":
        tag, value = self.read_any()
        if tag != expected_tag:
            raise SnmpError(f"expected BER tag 0x{expected_tag:02x}, got 0x{tag:02x}")
        return BerReader(value)

    def read_int(self) -> int:
        tag, value = self.read_any()
        if tag != 0x02:
            raise SnmpError(f"expected integer, got tag 0x{tag:02x}")
        return decode_signed_int(value)

    def read_octet_string(self) -> bytes:
        tag, value = self.read_any()
        if tag != 0x04:
            raise SnmpError(f"expected octet string, got tag 0x{tag:02x}")
        return value

    def read_oid(self) -> OID:
        tag, value = self.read_any()
        if tag != 0x06:
            raise SnmpError(f"expected oid, got tag 0x{tag:02x}")
        return decode_oid(value)

    def _read_length(self) -> int:
        first = self.data[self.pos]
        self.pos += 1
        if first < 0x80:
            return first
        count = first & 0x7F
        if count == 0 or count > 4:
            raise SnmpError("unsupported BER length")
        if self.pos + count > len(self.data):
            raise SnmpError("truncated BER length")
        value = int.from_bytes(self.data[self.pos : self.pos + count], "big")
        self.pos += count
        return value


def encode_tlv(tag: int, value: bytes) -> bytes:
    return bytes([tag]) + encode_length(len(value)) + value


def encode_length(length: int) -> bytes:
    if length < 0x80:
        return bytes([length])
    raw = length.to_bytes((length.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(raw)]) + raw


def encode_sequence(value: bytes) -> bytes:
    return encode_tlv(0x30, value)


def encode_integer(value: int) -> bytes:
    if value == 0:
        raw = b"\x00"
    else:
        raw = value.to_bytes((value.bit_length() + 7) // 8, "big", signed=False)
        if raw[0] & 0x80:
            raw = b"\x00" + raw
    return encode_tlv(0x02, raw)


def encode_octet_string(value: bytes) -> bytes:
    return encode_tlv(0x04, value)


def encode_null() -> bytes:
    return b"\x05\x00"


def encode_oid(oid: OID) -> bytes:
    if len(oid) < 2:
        raise ValueError("OID must contain at least two arcs")
    first = bytes([oid[0] * 40 + oid[1]])
    rest = b"".join(encode_base128(part) for part in oid[2:])
    return encode_tlv(0x06, first + rest)


def encode_base128(value: int) -> bytes:
    if value < 0:
        raise ValueError("OID arc cannot be negative")
    parts = [value & 0x7F]
    value >>= 7
    while value:
        parts.append(0x80 | (value & 0x7F))
        value >>= 7
    return bytes(reversed(parts))


def decode_signed_int(data: bytes) -> int:
    if not data:
        return 0
    return int.from_bytes(data, "big", signed=bool(data[0] & 0x80))


def decode_unsigned_int(data: bytes) -> int:
    return int.from_bytes(data, "big", signed=False)


def decode_text(data: bytes) -> str:
    for encoding in ("utf-8", "gb18030", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.hex()


def parse_oid(value: str | OID) -> OID:
    if isinstance(value, tuple):
        return value
    return tuple(int(part) for part in value.strip(".").split(".") if part)


def decode_oid(data: bytes) -> OID:
    if not data:
        return ()
    first = data[0]
    oid = [first // 40, first % 40]
    value = 0
    for byte in data[1:]:
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            oid.append(value)
            value = 0
    return tuple(oid)


def oid_starts_with(value: OID, prefix: OID) -> bool:
    return value[: len(prefix)] == prefix
