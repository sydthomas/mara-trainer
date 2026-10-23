"""Tiny synthetic FIT encoder for tests (no real personal data, no fixture files on disk).

Writes a valid FIT 2.0 file with a file_id message and one session message per run.
"""

from __future__ import annotations

import struct
from datetime import UTC, datetime

FIT_EPOCH = 631065600  # 1989-12-31T00:00:00Z
SPORTS = {"running": 1, "cycling": 2, "walking": 11}

_CRC_TABLE = [
    0x0000,
    0xCC01,
    0xD801,
    0x1400,
    0xF001,
    0x3C00,
    0x2800,
    0xE401,
    0xA001,
    0x6C00,
    0x7800,
    0xB401,
    0x5000,
    0x9C01,
    0x8801,
    0x4400,
]


def crc16(data: bytes, crc: int = 0) -> int:
    for byte in data:
        tmp = _CRC_TABLE[crc & 0xF]
        crc = (crc >> 4) & 0x0FFF
        crc = crc ^ tmp ^ _CRC_TABLE[byte & 0xF]
        tmp = _CRC_TABLE[crc & 0xF]
        crc = (crc >> 4) & 0x0FFF
        crc = crc ^ tmp ^ _CRC_TABLE[(byte >> 4) & 0xF]
    return crc


def _fit_ts(dt: datetime) -> int:
    return int(dt.astimezone(UTC).timestamp()) - FIT_EPOCH


def build_fit(sessions: list[dict[str, object]]) -> bytes:
    """sessions: [{"start": datetime, "meters": float, "seconds": float, "sport": "running"}]"""
    body = bytearray()
    # file_id definition (local 0, global 0): type enum, manufacturer uint16, time_created uint32
    body += (
        bytes([0x40, 0, 0]) + struct.pack("<H", 0) + bytes([3, 0, 1, 0x00, 1, 2, 0x84, 4, 4, 0x86])
    )
    created = sessions[0]["start"] if sessions else datetime(2026, 1, 1, tzinfo=UTC)
    assert isinstance(created, datetime)
    body += bytes([0x00]) + struct.pack("<BHI", 4, 255, _fit_ts(created))
    # session definition (local 1, global 18)
    fields = [(253, 4, 0x86), (2, 4, 0x86), (5, 1, 0x00), (7, 4, 0x86), (8, 4, 0x86), (9, 4, 0x86)]
    body += bytes([0x41, 0, 0]) + struct.pack("<H", 18) + bytes([len(fields)])
    for num, size, base in fields:
        body += bytes([num, size, base])
    for s in sessions:
        start, meters, seconds = s["start"], s["meters"], s["seconds"]
        assert isinstance(start, datetime)
        assert isinstance(meters, (int, float)) and isinstance(seconds, (int, float))
        ts = _fit_ts(start)
        body += bytes([0x01]) + struct.pack(
            "<IIBIII",
            ts + int(seconds),
            ts,
            SPORTS[str(s.get("sport", "running"))],
            int(seconds * 1000),
            int(seconds * 1000),
            int(meters * 100),
        )
    header = struct.pack("<BBHI4s", 14, 0x20, 2132, len(body), b".FIT")
    header += struct.pack("<H", crc16(header))
    data = header + bytes(body)
    return data + struct.pack("<H", crc16(data))
