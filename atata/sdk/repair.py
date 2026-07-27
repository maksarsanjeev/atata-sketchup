"""Исправления, которые умеет только SDK: чистка модели и пересохранение.

Ключевое отличие от фиксов контейнерного слоя (:mod:`atata.fixes`): там мы
пересобирали ZIP руками и не трогали ``model.dat``, здесь файл целиком
пишет родной сериализатор SketchUp через ``SUModelSaveToFile``. Это
надёжнее — результат заведомо в том формате, который SketchUp сам и читает.
"""

from __future__ import annotations

import os
from ctypes import byref, c_size_t
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .capi import (
    SUComponentDefinitionRef,
    SUDrawingElementRef,
    SUEntityRef,
    SUModelRef,
    SUSceneRef,
    SdkError,
    call,
    get_array,
    load_sdk,
)
from .inspect import _ensure_initialized, _safe_count

# Удаление одного определения может освободить другое, вложенное в него,
# поэтому чистка идёт проходами до тех пор, пока находится что удалять.
MAX_PASSES = 20

# Чистить ли заодно неиспользуемые теги. Выключено: см. комментарий у вызова.
PURGE_LAYERS = os.environ.get("ATATA_PURGE_LAYERS", "0") == "1"


@dataclass
class PurgeReport:
    definitions_before: int = 0
    definitions_after: int = 0
    removed_definitions: int = 0
    removed_layers: int = 0
    removed_layer_folders: int = 0
    passes: int = 0
    size_before: int = 0
    size_after: int = 0
    scenes: int = 0
    cleared_scene_hidden: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def saved(self) -> int:
        return self.size_before - self.size_after

    def as_dict(self) -> dict:
        return {
            "definitions_before": self.definitions_before,
            "definitions_after": self.definitions_after,
            "removed_definitions": self.removed_definitions,
            "removed_layers": self.removed_layers,
            "removed_layer_folders": self.removed_layer_folders,
            "passes": self.passes,
            "size_before": self.size_before,
            "size_after": self.size_after,
            "saved": self.saved,
            "scenes": self.scenes,
            "cleared_scene_hidden": self.cleared_scene_hidden,
            "errors": self.errors,
        }


def purge_model(
    src: str | Path,
    dest: str | Path,
    progress: Callable[[str, float], None] | None = None,
) -> PurgeReport:
    """Выбросить неиспользуемые определения компонентов и пустые теги.

    Ровно то, что делает штатный Purge Unused в самом SketchUp: определения,
    которые ни разу не вставлены в модель, из файла удаляются. На геометрию
    сцены это не влияет — удаляется только то, чего на сцене нет.
    """
    src, dest = Path(src), Path(dest)
    report = PurgeReport(size_before=src.stat().st_size)

    lib = load_sdk()
    _ensure_initialized(lib)

    # Первый заход — не трогая сцены. На большинстве файлов этого хватает.
    if _attempt(lib, src, dest, report, clear_hidden=False, progress=progress):
        _finalize(report, dest, progress)
        return report

    # Не сохранилось: у сцен повисли ссылки на скрытые объекты. Пройти по
    # этому списку после чистки уже нельзя — там битые указатели, обращение
    # к ним роняет процесс. Поэтому начинаем заново и расчищаем сцены до.
    if progress:
        progress("сцены мешают сохранению, захожу заново", 0.4)

    report.removed_definitions = 0
    report.passes = 0
    if _attempt(lib, src, dest, report, clear_hidden=True, progress=progress):
        _finalize(report, dest, progress)
        return report

    report.errors.append("модель не сохранилась даже после расчистки сцен")
    _finalize(report, dest, progress)
    return report


def _finalize(report: PurgeReport, dest: Path, progress) -> None:
    if dest.exists():
        report.size_after = dest.stat().st_size
    elif not report.errors:
        report.errors.append("SDK не создал выходной файл")
    if progress:
        progress("готово", 1.0)


def _attempt(
    lib,
    src: Path,
    dest: Path,
    report: PurgeReport,
    clear_hidden: bool,
    progress: Callable[[str, float], None] | None,
) -> bool:
    """Один заход: загрузить, почистить, сохранить. True — получилось."""
    model = SUModelRef()
    call(lib, "SUModelCreateFromFile", byref(model), str(src).encode("utf-8"))
    try:
        report.definitions_before = _safe_count(
            lib, "SUModelGetNumComponentDefinitions", model
        )
        report.scenes = _safe_count(lib, "SUModelGetNumScenes", model)

        if clear_hidden:
            report.cleared_scene_hidden = _clear_scene_hidden(lib, model, report)

        for attempt in range(MAX_PASSES):
            layer = _removable(lib, model)
            if not layer:
                break

            if progress:
                progress(
                    f"чищу неиспользуемое, проход {attempt + 1}",
                    min(0.7, 0.1 + attempt * 0.05),
                )

            array = (SUComponentDefinitionRef * len(layer))(*layer)
            call(lib, "SUModelRemoveComponentDefinitions", model, len(layer), array)
            report.removed_definitions += len(layer)
            report.passes = attempt + 1
        else:
            report.errors.append(
                f"чистка не сошлась за {MAX_PASSES} проходов — часть мусора "
                f"осталась, можно прогнать ещё раз"
            )

        report.definitions_after = _safe_count(
            lib, "SUModelGetNumComponentDefinitions", model
        )

        # Чистка тегов — под флагом: каждая сцена хранит собственный список
        # видимости тегов, и удаление тега оставляет там висячую ссылку.
        if PURGE_LAYERS:
            report.removed_layers = _purge(
                lib, model, "SUModelPurgeUnusedLayers", report
            )
            report.removed_layer_folders = _purge(
                lib, model, "SUModelPurgeEmptyLayerFolders", report
            )

        if progress:
            progress("сохраняю модель", 0.9 if clear_hidden else 0.35)
        if dest.exists():
            dest.unlink()
        try:
            call(lib, "SUModelSaveToFile", model, str(dest).encode("utf-8"))
            return True
        except SdkError as exc:
            if "SERIALIZATION" in str(exc) and not clear_hidden:
                return False
            raise
    finally:
        try:
            call(lib, "SUModelRelease", byref(model))
        except SdkError:
            pass


def _unused_definitions(lib, model) -> list:
    count = _safe_count(lib, "SUModelGetNumComponentDefinitions", model)
    if not count:
        return []
    refs = get_array(
        lib, "SUModelGetComponentDefinitions", model, count, SUComponentDefinitionRef
    )
    unused = []
    for ref in refs:
        # GetNumUsedInstances считает вставки, реально присутствующие в
        # модели; у мусорных определений он даёт ноль.
        if _safe_count(lib, "SUComponentDefinitionGetNumUsedInstances", ref) == 0:
            unused.append(ref)
    return unused


def _removable(lib, model) -> list:
    """Определения, которые можно снять на текущем проходе.

    Берём только те, у которых нет вставок ВООБЩЕ. Определение с
    ``used_instances == 0``, но ``instances > 0`` лежит внутри другого
    неиспользуемого определения; если снести оба разом, вложенное
    освобождается, пока родитель на него ещё ссылается. Поэтому чистим
    слоями: сперва внешний слой, потом пересчитываем.
    """
    count = _safe_count(lib, "SUModelGetNumComponentDefinitions", model)
    if not count:
        return []
    refs = get_array(
        lib, "SUModelGetComponentDefinitions", model, count, SUComponentDefinitionRef
    )
    return [
        ref
        for ref in refs
        if _safe_count(lib, "SUComponentDefinitionGetNumInstances", ref) == 0
        and _safe_count(lib, "SUComponentDefinitionGetNumUsedInstances", ref) == 0
    ]


def _clear_scene_hidden(lib, model, report: PurgeReport) -> int:
    """Снять со сцен пометки «скрыто».

    Вызывается ТОЛЬКО до удаления определений. После — в списке скрытых
    объектов остаются висячие указатели, и обращение к ним роняет процесс
    access violation-ом, а не аккуратной ошибкой SDK.
    """
    cleared = 0
    count = _safe_count(lib, "SUModelGetNumScenes", model)
    if not count:
        return 0

    for scene in get_array(lib, "SUModelGetScenes", model, count, SUSceneRef):
        hidden_count = _safe_count(lib, "SUSceneGetNumHiddenEntities", scene)
        if not hidden_count:
            continue
        entities = get_array(
            lib, "SUSceneGetHiddenEntities", scene, hidden_count, SUEntityRef
        )
        for entity in entities:
            # Все SU*Ref — это struct { void* ptr }, поэтому приведение типа
            # сводится к переносу указателя; штатные From/To объявлены в
            # заголовках как inline и из DLL не экспортируются.
            element = SUDrawingElementRef(entity.ptr)
            try:
                call(lib, "SUSceneSetDrawingElementHidden", scene, element, False)
                cleared += 1
            except SdkError:
                pass

    if cleared:
        report.errors.append(
            f"в сценах снята пометка «скрыто» с {cleared} объектов — иначе файл "
            f"не сохранялся. Сами сцены сохранены, но скрытое в них стало видимым."
        )
    return cleared


def _purge(lib, model, name: str, report: PurgeReport) -> int:
    try:
        value = c_size_t()
        call(lib, name, model, byref(value))
        return value.value
    except SdkError as exc:
        report.errors.append(f"{name}: {exc}")
        return 0
