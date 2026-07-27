"""Сбор фактов о файле .skp на уровне контейнера.

Здесь нет ни одного суждения о том, что «хорошо» и что «плохо» — только
измерения. Выводы делают правила в :mod:`atata.rules`.
"""

from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from PIL import Image

from . import meta as meta_mod
from .container import Entry, SkpContainer

# Pillow ругается на большие текстуры как на возможную decompression bomb.
# В .skp это норма жизни, поэтому предел поднимаем, но не снимаем совсем.
Image.MAX_IMAGE_PIXELS = 512_000_000

TEXTURE_EXT = (
    ".jpg", ".jpeg", ".png", ".tif", ".tiff",
    ".bmp", ".tga", ".gif", ".psd", ".webp",
)

_MATERIAL_TAG = re.compile(rb"<mat:material\s([^>]*)>", re.DOTALL)
_ATTR = re.compile(rb'(\w+)="([^"]*)"')


@dataclass
class TextureInfo:
    entry: str  # полный путь внутри контейнера
    material: str
    filename: str
    bytes: int
    width: int | None = None
    height: int | None = None
    fmt: str | None = None
    mode: str | None = None
    sha1: str = ""
    dhash: int | None = None
    unreadable: str | None = None

    @property
    def has_alpha(self) -> bool:
        return self.mode in ("RGBA", "LA", "PA", "P") if self.mode else False

    @property
    def megapixels(self) -> float:
        if not self.width or not self.height:
            return 0.0
        return self.width * self.height / 1_000_000


@dataclass
class MaterialInfo:
    name: str
    xml_entry: str | None = None
    has_texture: bool = False
    textures: list[str] = field(default_factory=list)
    has_thumbnail: bool = False
    color: tuple[int, int, int] | None = None
    opacity: float | None = None
    attrs: dict[str, str] = field(default_factory=dict)


@dataclass
class SkpFacts:
    path: Path
    filename: str
    file_size: int
    version: str | None
    units: str | None
    source_path: str | None
    entry_count: int
    model_dat_size: int
    model_dat_compressed: int
    materials: list[MaterialInfo]
    textures: list[TextureInfo]
    group_sizes: dict[str, int]
    group_counts: dict[str, int]
    thumbnail_count: int
    style_count: int
    scene_count: int

    @property
    def texture_bytes(self) -> int:
        return sum(t.bytes for t in self.textures)

    def material(self, name: str) -> MaterialInfo | None:
        for m in self.materials:
            if m.name == name:
                return m
        return None


def collect_facts(
    path: str | Path,
    progress: Callable[[str, float], None] | None = None,
) -> SkpFacts:
    """Прочитать контейнер и собрать все измеримые факты."""
    path = Path(path)

    def report(stage: str, frac: float) -> None:
        if progress:
            progress(stage, frac)

    with SkpContainer(path) as skp:
        entries = skp.entries
        report("читаю оглавление", 0.05)

        group_sizes: dict[str, int] = {}
        group_counts: dict[str, int] = {}
        model_dat_size = 0
        model_dat_compressed = 0
        for e in entries:
            group_sizes[e.top] = group_sizes.get(e.top, 0) + e.size
            group_counts[e.top] = group_counts.get(e.top, 0) + 1
            if e.name == "model.dat":
                model_dat_size = e.size
                model_dat_compressed = e.compressed_size

        # -- метаданные -----------------------------------------------------
        parsed_meta = meta_mod.SkpMeta()
        if any(e.name == "meta/meta.dat" for e in entries):
            try:
                parsed_meta = meta_mod.parse(skp.read("meta/meta.dat"))
            except Exception:
                pass

        # -- материалы ------------------------------------------------------
        report("разбираю материалы", 0.15)
        materials = _collect_materials(skp, entries)

        # -- текстуры -------------------------------------------------------
        texture_entries = [e for e in entries if _is_texture(e.name)]
        textures: list[TextureInfo] = []
        total = max(len(texture_entries), 1)
        for index, e in enumerate(texture_entries):
            textures.append(_probe_texture(skp, e))
            if index % 20 == 0:
                report("измеряю текстуры", 0.2 + 0.75 * index / total)

        report("готово", 1.0)

        return SkpFacts(
            path=path,
            filename=path.name,
            file_size=skp.file_size,
            version=parsed_meta.version_string or skp.version,
            units=parsed_meta.units,
            source_path=parsed_meta.source_path,
            entry_count=len(entries),
            model_dat_size=model_dat_size,
            model_dat_compressed=model_dat_compressed,
            materials=materials,
            textures=textures,
            group_sizes=group_sizes,
            group_counts=group_counts,
            thumbnail_count=group_counts.get("thumbnails", 0),
            style_count=group_counts.get("styles", 0),
            scene_count=group_counts.get("scene_thumbnails", 0),
        )


def _is_texture(name: str) -> bool:
    if not name.startswith("materials/"):
        return False
    leaf = name.rsplit("/", 1)[-1].lower()
    if leaf in ("material.xml", "thumbnail.jpg"):
        return False
    return leaf.endswith(TEXTURE_EXT)


def _collect_materials(skp: SkpContainer, entries: list[Entry]) -> list[MaterialInfo]:
    by_name: dict[str, MaterialInfo] = {}

    for e in entries:
        if not e.name.startswith("materials/"):
            continue
        parts = e.name.split("/")
        if len(parts) < 2:
            continue
        name = parts[1]
        info = by_name.setdefault(name, MaterialInfo(name=name))
        leaf = parts[-1].lower()
        if leaf == "material.xml":
            info.xml_entry = e.name
        elif leaf == "thumbnail.jpg":
            info.has_thumbnail = True
        elif _is_texture(e.name):
            info.textures.append(e.name)

    # Атрибуты тянем регуляркой, а не XML-парсером: файлов больше тысячи,
    # нужен только открывающий тег, и часть файлов бывает с битым хвостом.
    for info in by_name.values():
        if not info.xml_entry:
            continue
        try:
            raw = skp.read(info.xml_entry)
        except Exception:
            continue
        match = _MATERIAL_TAG.search(raw)
        if not match:
            continue
        attrs = {
            k.decode("ascii", "replace"): v.decode("utf-8", "replace")
            for k, v in _ATTR.findall(match.group(1))
        }
        info.attrs = attrs
        info.has_texture = attrs.get("hasTexture") == "1"
        try:
            info.color = (
                int(attrs["colorRed"]),
                int(attrs["colorGreen"]),
                int(attrs["colorBlue"]),
            )
        except (KeyError, ValueError):
            pass
        try:
            if attrs.get("useTrans") == "1":
                info.opacity = float(attrs["trans"])
        except (KeyError, ValueError):
            pass

    return sorted(by_name.values(), key=lambda m: m.name)


def _probe_texture(skp: SkpContainer, entry: Entry) -> TextureInfo:
    parts = entry.name.split("/")
    info = TextureInfo(
        entry=entry.name,
        material=parts[1] if len(parts) > 1 else "?",
        filename=parts[-1],
        bytes=entry.size,
    )
    try:
        data = skp.read(entry.name)
    except Exception as exc:
        info.unreadable = f"не читается из архива: {exc}"
        return info

    info.sha1 = hashlib.sha1(data).hexdigest()
    try:
        with Image.open(io.BytesIO(data)) as im:
            info.width, info.height = im.size
            info.fmt = im.format
            info.mode = im.mode
            info.dhash = _dhash(im)
    except Exception as exc:
        info.unreadable = f"не распознаётся как изображение: {exc}"
    return info


def _dhash(im: Image.Image, size: int = 8) -> int | None:
    """Перцептивный хеш: соседние пиксели уменьшенной картинки.

    Побайтовое сравнение почти бесполезно — одна и та же текстура обычно
    пересохранена с другим качеством JPEG. dhash ловит именно такие пары.
    """
    try:
        # draft() для JPEG декодирует сразу в уменьшенном виде — на текстурах
        # 4000x2250 это разница в порядок по времени и памяти.
        if im.format == "JPEG":
            im.draft("L", (size * 4, size * 4))
        small = im.convert("L").resize((size + 1, size), Image.BILINEAR)
    except Exception:
        return None

    pixels = list(small.getdata())
    bits = 0
    for row in range(size):
        base = row * (size + 1)
        for col in range(size):
            bits <<= 1
            if pixels[base + col] > pixels[base + col + 1]:
                bits |= 1
    return bits


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")
