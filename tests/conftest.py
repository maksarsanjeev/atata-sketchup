"""Сборка синтетических .skp для тестов.

Настоящие файлы в репозиторий не кладём: они весят сотни мегабайт и почти
всегда чужие. Вместо этого собираем контейнер той же структуры вручную.
"""

from __future__ import annotations

import io
import struct
import zipfile
from pathlib import Path

import pytest
from PIL import Image

MATERIAL_XML = """<?xml version="1.0" encoding="UTF-8" standalone="no" ?>
<materialDocument xmlns:mat="http://sketchup.google.com/schemas/sketchup/1.0/material">
  <mat:material name="{name}" type="0" colorRed="{r}" colorGreen="{g}" colorBlue="{b}" \
trans="0.5" useTrans="0" hasTexture="{has_texture}">
  </mat:material>
</materialDocument>
"""


def build_prefix(version: str = "24.0.484") -> bytes:
    """Тот же заголовок, что пишет SketchUp: две UTF-16LE строки с длиной."""

    def tagged(text: str) -> bytes:
        return b"\xff\xfe\xff" + bytes([len(text)]) + text.encode("utf-16-le")

    return (
        tagged("SketchUp Model")
        + tagged("{" + version + "}")
        + b"VFF"
        + b"\x08\x00\x01\x00\x0e\x00\xae\x00\xb5\x16"
    )


def build_meta(version: str, units: str, source: str) -> bytes:
    def tlv(key: str, value: bytes) -> bytes:
        return struct.pack("<HI", ord(key), len(value)) + value

    body = (
        tlv("u", version.encode())
        + tlv("v", struct.pack("<H", 24))
        + tlv("m", units.encode())
        + tlv("o", source.encode())
    )
    return struct.pack("<HI", ord("d"), len(body)) + body


def make_image(width: int, height: int, fmt: str = "JPEG", mode: str = "RGB") -> bytes:
    # Шум, а не заливка: иначе JPEG сожмётся в килобайт и тесты на вес
    # перестанут что-либо проверять.
    img = Image.new(mode, (width, height))
    pixels = img.load()
    for y in range(0, height, 3):
        for x in range(0, width, 3):
            value = (x * 7 + y * 13) % 256
            pixels[x, y] = (value, (value * 3) % 256, (value * 7) % 256)[
                : len(img.getbands())
            ]
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    return buf.getvalue()


def make_skp(
    path: Path,
    textures: list[tuple[str, str, int, int, str]] | None = None,
    extra_materials: list[str] | None = None,
    model_bytes: int = 64 * 1024,
    version: str = "24.0.484",
    source: str = r"\\office\projects\somebody\model.skp",
) -> Path:
    """Собрать .skp: префикс + ZIP с model.dat, meta и материалами."""
    textures = textures or []
    extra_materials = extra_materials or []

    with open(path, "wb") as fh:
        fh.write(build_prefix(version))
        with zipfile.ZipFile(fh, "w") as z:
            z.writestr("meta/meta.dat", build_meta(version, "Millimeter", source))

            info = zipfile.ZipInfo("model.dat")
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, (b"GEOMETRY-BLOCK-" * 64)[:64] * (model_bytes // 64))

            named = {name for name, *_ in textures} | set(extra_materials)
            for name in sorted(named):
                has_texture = any(name == m for m, *_ in textures)
                z.writestr(
                    f"materials/{name}/material.xml",
                    MATERIAL_XML.format(
                        name=name, r=112, g=90, b=64, has_texture=int(has_texture)
                    ),
                )

            for material, filename, width, height, fmt in textures:
                data = make_image(width, height, fmt)
                stored = zipfile.ZipInfo(f"materials/{material}/{filename}")
                stored.compress_type = zipfile.ZIP_STORED
                z.writestr(stored, data)

    return path


@pytest.fixture
def simple_skp(tmp_path: Path) -> Path:
    return make_skp(
        tmp_path / "simple.skp",
        textures=[("Wood", "wood.jpg", 512, 512, "JPEG")],
        extra_materials=["Glass", "Steel"],
    )


@pytest.fixture
def messy_skp(tmp_path: Path) -> Path:
    """Файл с теми же болезнями, что у реальных проектов."""
    return make_skp(
        tmp_path / "messy.skp",
        textures=[
            ("Concrete", "concrete.jpg", 3000, 2400, "JPEG"),  # переразмеренная
            ("Concrete2", "concrete.jpg", 3000, 2400, "JPEG"),  # точный дубль
            ("Wood", "wood.jpg", 1024, 1024, "JPEG"),
            ("Banner", "banner.jpg", 4000, 500, "JPEG"),  # экстремальные пропорции
        ],
        extra_materials=[
            "_auto_", "_auto_1", "_auto_2",
            "Material1", "Material2", "Material3",
            "Image", "Image2", "Image3",
        ],
    )
