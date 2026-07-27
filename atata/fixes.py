"""Применение исправлений.

**Править контейнер напрямую нельзя.** Пересборка ZIP ломает файл, даже
если не менять в нём ни байта содержимого: проверено на рабочем файле —
все 2340 записей совпадают по именам, размерам, CRC и способу сжатия,
префикс идентичен, а SketchUp такой файл уже не открывает. Похоже, его
читатель опирается на абсолютные смещения, а не на каталог ZIP: стоит
`model.dat` сжаться иначе, и всё уезжает.

Поэтому единственный рабочий способ что-то исправить — пересохранить
модель через ``SUModelSaveToFile``, то есть родным сериализатором
SketchUp. Всё, что делает этот модуль, идёт через SDK.

Результат обязательно проверяется на открываемость: целый ZIP ничего не
гарантирует, единственная честная проверка — открыть файл тем же
читателем, что и SketchUp.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .skp.container import verify
from .skp.facts import SkpFacts


@dataclass
class FixSpec:
    id: str
    label: str
    description: str
    kind: str = "sdk"


AVAILABLE_FIXES: dict[str, FixSpec] = {
    "fix_errors": FixSpec(
        id="fix_errors",
        label="Починить ошибки модели",
        description=(
            "Штатная проверка и ремонт внутренних ошибок — то же, что "
            "«Fix Problems» в самом SketchUp."
        ),
    ),
    "erase_loose_edges": FixSpec(
        id="erase_loose_edges",
        label="Убрать висячие рёбра",
        description=(
            "Удаляет рёбра, не принадлежащие ни одной грани: следы построения "
            "и остатки импорта."
        ),
    ),
    "normalize_textures": FixSpec(
        id="normalize_textures",
        label="Текстуры — к степени двойки",
        description=(
            "Видеокарта дополняет текстуру до ближайшей степени двойки, поэтому "
            "4000×2250 занимает в видеопамяти как 4096×4096. Приведение к точным "
            "степеням двойки снимает этот перерасход. Привязка текстуры к "
            "размерам в модели сохраняется."
        ),
    ),
    "purge_unused": FixSpec(
        id="purge_unused",
        label="Вычистить неиспользуемое",
        description=(
            "Удаляет определения компонентов, не вставленные в модель. То же, "
            "что штатный Purge Unused. Они занимают память при каждом открытии."
        ),
    ),
}


@dataclass
class FixReport:
    dest: Path
    applied: list[str]
    size_before: int = 0
    size_after: int = 0
    verified: bool = False
    verify_message: str = ""
    openable: bool | None = None
    open_message: str = ""
    errors: list[str] = field(default_factory=list)
    repair: dict | None = None

    @property
    def saved(self) -> int:
        return self.size_before - self.size_after

    @property
    def usable(self) -> bool:
        """Можно ли отдавать файл пользователю."""
        return self.verified and self.openable is not False

    def as_dict(self) -> dict:
        return {
            "applied": self.applied,
            "size_before": self.size_before,
            "size_after": self.size_after,
            "saved": self.saved,
            "verified": self.verified,
            "verify_message": self.verify_message,
            "openable": self.openable,
            "open_message": self.open_message,
            "usable": self.usable,
            "errors": self.errors[:20],
            "filename": self.dest.name,
            "repair": self.repair,
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
    report = FixReport(dest=dest, applied=fix_ids, size_before=src.stat().st_size)

    if not fix_ids:
        report.errors.append("не выбрано ни одного применимого исправления")
        return report

    from .sdk import can_open, repair_geometry

    repair_report, error = repair_geometry(src, dest, fix_ids, progress=progress)
    if error:
        report.errors.append(f"правка не выполнена: {error}")
        return report
    report.repair = repair_report

    if not dest.exists():
        report.errors.append("на выходе не оказалось файла")
        return report

    report.size_after = dest.stat().st_size

    if progress:
        progress("проверяю целостность контейнера", 0.93)
    report.verified, report.verify_message = verify(dest)

    if progress:
        progress("проверяю, откроется ли в SketchUp", 0.96)
    report.openable, open_error = can_open(dest)
    if report.openable is None:
        report.open_message = f"проверить нечем: {open_error}"
    elif report.openable:
        report.open_message = "файл открывается читателем SketchUp"
    else:
        report.open_message = f"файл НЕ открывается: {open_error}"
        report.errors.append(
            "результат не прошёл проверку открываемости — скачивать его нельзя"
        )

    if progress:
        progress("готово", 1.0)
    return report
