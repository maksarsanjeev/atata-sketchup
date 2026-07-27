"""Разбор ``meta/meta.dat`` — блока метаданных контейнера .skp.

Формат простой TLV: двухбайтовый ключ (little-endian), четырёхбайтовая длина,
затем значение. Весь блок обёрнут в одну запись с ключом ``d``, поэтому разбор
начинается со смещения 6.

Ключи определены эмпирически на файлах SketchUp 2024, набор заведомо неполный —
всё неизвестное просто складывается в ``raw``.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

_HEADER_LEN = 6

KEY_VERSION_STRING = ord("u")  # "24.0.484"
KEY_VERSION_MAJOR = ord("v")  # uint16, 24
KEY_UNITS = ord("m")  # "Millimeter"
KEY_SOURCE_PATH = ord("o")  # полный путь, откуда файл сохраняли
KEY_GUID = ord("f")


@dataclass
class SkpMeta:
    version_string: str | None = None
    version_major: int | None = None
    units: str | None = None
    source_path: str | None = None
    guid: str | None = None
    raw: dict[int, bytes] = field(default_factory=dict)


def parse(data: bytes) -> SkpMeta:
    meta = SkpMeta()
    if len(data) <= _HEADER_LEN:
        return meta

    pos = _HEADER_LEN
    end = len(data)
    while pos + 6 <= end:
        key, length = struct.unpack_from("<HI", data, pos)
        pos += 6
        if length > end - pos:
            break  # мусор в хвосте — дальше не идём
        value = data[pos : pos + length]
        pos += length
        meta.raw[key] = value

        if key == KEY_VERSION_STRING:
            meta.version_string = _text(value)
        elif key == KEY_VERSION_MAJOR and length >= 2:
            meta.version_major = struct.unpack_from("<H", value, 0)[0]
        elif key == KEY_UNITS:
            meta.units = _text(value)
        elif key == KEY_SOURCE_PATH:
            meta.source_path = _text(value)
        elif key == KEY_GUID:
            meta.guid = value.hex()

    return meta


def _text(value: bytes) -> str | None:
    for encoding in ("utf-8", "cp1251", "latin-1"):
        try:
            return value.decode(encoding).rstrip("\x00")
        except UnicodeDecodeError:
            continue
    return None
