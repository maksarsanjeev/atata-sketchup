"""Применение исправлений.

Пока здесь живут только фиксы уровня контейнера — те, что меняют картинки
внутри ZIP, не трогая ``model.dat``. Всё, что требует переназначения
материалов на гранях или purge неиспользуемого, ждёт этапа SketchUp SDK.

Важно про проверку результата: после пересборки контейнер проверяется на
целостность (все записи читаются, `model.dat` на месте, заголовок цел).
Это **не** гарантия, что SketchUp примет файл — round-trip через сам
SketchUp не проверялся, потому что его нет в контуре сборки. Оригинал
всегда сохраняется рядом.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from PIL import Image

from .rules import MAX_TEXTURE_DIM
from .skp.container import Entry, SkpContainer, verify
from .skp.facts import SkpFacts

JPEG_QUALITY = 88


@dataclass
class FixSpec:
    id: str
    label: str
    description: str
    kind: str = "auto"


AVAILABLE_FIXES: dict[str, FixSpec] = {
    "downscale_textures": FixSpec(
        id="downscale_textures",
        label=f"Ужать текстуры до {MAX_TEXTURE_DIM}px",
        description=(
            f"Уменьшает длинную сторону каждой текстуры до {MAX_TEXTURE_DIM}px. "
            f"Формат и имя файла сохраняются, ссылки из model.dat не ломаются."
        ),
    ),
    "normalize_pot": FixSpec(
        id="normalize_pot",
        label="Привести размеры к степени двойки",
        description=(
            "Округляет стороны вниз до ближайшей степени двойки. Немного меняет "
            "пропорции — на бесшовных паттернах проверьте результат."
        ),
    ),
}


@dataclass
class FixReport:
    dest: Path
    applied: list[str]
    touched: list[str] = field(default_factory=list)
    size_before: int = 0
    size_after: int = 0
    verified: bool = False
    verify_message: str = ""
    errors: list[str] = field(default_factory=list)

    @property
    def saved(self) -> int:
        return self.size_before - self.size_after

    def as_dict(self) -> dict:
        return {
            "applied": self.applied,
            "touched": self.touched[:60],
            "touched_total": len(self.touched),
            "size_before": self.size_before,
            "size_after": self.size_after,
            "saved": self.saved,
            "verified": self.verified,
            "verify_message": self.verify_message,
            "errors": self.errors[:20],
            "filename": self.dest.name,
        }


def apply_fixes(
    src: str | Path,
    dest: str | Path,
    fix_ids: list[str],
    facts: SkpFacts,
    progress: Callable[[str, float], None] | None = None,
) -> FixReport:
    src, dest = Path(src), Path(dest)
    fix_ids = [fid for fid in fix_ids if fid in AVAILABLE_FIXES]
    report = FixReport(dest=dest, applied=fix_ids)

    if not fix_ids:
        report.errors.append("не выбрано ни одного применимого исправления")
        return report

    targets = _plan(facts, fix_ids)
    if not targets:
        report.errors.append("под выбранные исправления не попала ни одна текстура")
        return report

    def transform(entry: Entry, data: bytes) -> bytes | None:
        target = targets.get(entry.name)
        if target is None:
            return None
        try:
            new_data = _resize_image(data, target)
        except Exception as exc:
            report.errors.append(f"{entry.name}: {type(exc).__name__}: {exc}")
            return None
        # Если «оптимизация» сделала файл тяжелее — оставляем как было.
        if new_data is None or len(new_data) >= len(data):
            return None
        report.touched.append(entry.name)
        return new_data

    def on_progress(done: int, total: int) -> None:
        if progress and total:
            progress("пересобираю контейнер", done / total)

    with SkpContainer(src) as container:
        result = container.rebuild(
            dest,
            # В память читаем только текстуры, которые реально меняем.
            # Всё прочее — включая гигабайтный model.dat — идёт потоком.
            selector=lambda entry: entry.name in targets,
            transform=transform,
            progress=on_progress,
        )
    report.size_before = result.size_before
    report.size_after = result.size_after

    if progress:
        progress("проверяю целостность", 1.0)
    report.verified, report.verify_message = verify(dest)
    return report


def _plan(facts: SkpFacts, fix_ids: list[str]) -> dict[str, tuple[int, int]]:
    """Посчитать целевой размер для каждой текстуры, которую надо тронуть."""
    targets: dict[str, tuple[int, int]] = {}

    for t in facts.textures:
        if not t.width or not t.height or t.unreadable:
            continue
        w, h = t.width, t.height

        if "downscale_textures" in fix_ids:
            longest = max(w, h)
            if longest > MAX_TEXTURE_DIM:
                scale = MAX_TEXTURE_DIM / longest
                w = max(1, round(w * scale))
                h = max(1, round(h * scale))

        if "normalize_pot" in fix_ids:
            w = _floor_pot(w)
            h = _floor_pot(h)

        if (w, h) != (t.width, t.height):
            targets[t.entry] = (w, h)

    return targets


def _floor_pot(value: int) -> int:
    if value < 1:
        return 1
    return 1 << (value.bit_length() - 1)


def _resize_image(data: bytes, size: tuple[int, int]) -> bytes | None:
    with Image.open(io.BytesIO(data)) as im:
        fmt = im.format or "PNG"
        icc = im.info.get("icc_profile")
        resized = im.convert(_target_mode(im.mode, fmt)).resize(size, Image.LANCZOS)

    buf = io.BytesIO()
    params: dict = {}
    if icc:
        params["icc_profile"] = icc

    if fmt == "JPEG":
        params.update(quality=JPEG_QUALITY, optimize=True, progressive=True)
    elif fmt == "PNG":
        params.update(optimize=True, compress_level=9)
    elif fmt in ("TIFF", "WEBP"):
        pass
    else:
        # Экзотику не переписываем: риск получить файл, который SketchUp
        # не откроет, выше возможной экономии.
        return None

    resized.save(buf, format=fmt, **params)
    return buf.getvalue()


def _target_mode(mode: str, fmt: str) -> str:
    if fmt == "JPEG":
        return "RGB" if mode not in ("L", "RGB", "CMYK") else mode
    if mode == "P":
        return "RGBA"
    return mode
