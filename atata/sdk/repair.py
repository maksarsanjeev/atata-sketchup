"""Исправления, которые умеет только SDK.

Все операции выполняются за один сеанс: модель открывается один раз
(20 с на файле 348 МБ), к ней применяется всё выбранное, и она один раз
сохраняется родным сериализатором SketchUp.

Задача этих правок — устойчивость, а не вес файла: снять нагрузку на
память и видеопамять, убрать явный мусор и починить внутренние ошибки.
"""

from __future__ import annotations

import ctypes
import os
import tempfile
from ctypes import byref, c_bool, c_double, c_size_t
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .capi import (
    SUComponentDefinitionRef,
    SUDrawingElementRef,
    SUEdgeRef,
    SUEntitiesRef,
    SUEntityRef,
    SUGroupRef,
    SUImageRepRef,
    SUMaterialRef,
    SUModelRef,
    SUSceneRef,
    SUTextureRef,
    SdkError,
    call,
    get_array,
    load_sdk,
    read_string,
)
from .inspect import _ensure_initialized, _safe_count

# Порядок фиксирован и осмыслен: сперва чиним, потом убираем мусор, потом
# правим текстуры, и только в конце выбрасываем неиспользуемое — к этому
# моменту предыдущие шаги могли что-то освободить.
OPERATION_ORDER = (
    "fix_errors",
    "erase_loose_edges",
    "normalize_textures",
    "purge_unused",
)

MAX_PASSES = 20

# Потолок стороны текстуры. Видеокарта дополняет текстуру до ближайшей
# степени двойки, поэтому целимся именно в них: 4000x2250 занимает в
# видеопамяти как 4096x4096, то есть 67 МБ на одну картинку.
MAX_TEXTURE_DIM = int(os.environ.get("ATATA_MAX_TEXTURE_DIM", "2048"))

# Чистка тегов ломает сохранение: сцены держат список видимости тегов,
# и удаление тега оставляет там висячую ссылку.
PURGE_LAYERS = os.environ.get("ATATA_PURGE_LAYERS", "0") == "1"


@dataclass
class RepairReport:
    operations: list[str] = field(default_factory=list)
    # починка
    fix_errors_ran: bool = False
    # висячие рёбра
    erased_edges: int = 0
    skipped_linework: int = 0
    # текстуры
    textures_seen: int = 0
    textures_resized: int = 0
    textures_failed: int = 0
    texture_scale_kept: bool = True
    # чистка
    definitions_before: int = 0
    definitions_after: int = 0
    removed_definitions: int = 0
    removed_layers: int = 0
    passes: int = 0
    # сцены
    scenes: int = 0
    cleared_scene_hidden: int = 0
    # файл
    size_before: int = 0
    size_after: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def saved(self) -> int:
        return self.size_before - self.size_after

    def as_dict(self) -> dict:
        return {
            "operations": self.operations,
            "fix_errors_ran": self.fix_errors_ran,
            "erased_edges": self.erased_edges,
            "skipped_linework": self.skipped_linework,
            "textures_seen": self.textures_seen,
            "textures_resized": self.textures_resized,
            "textures_failed": self.textures_failed,
            "texture_scale_kept": self.texture_scale_kept,
            "definitions_before": self.definitions_before,
            "definitions_after": self.definitions_after,
            "removed_definitions": self.removed_definitions,
            "removed_layers": self.removed_layers,
            "passes": self.passes,
            "scenes": self.scenes,
            "cleared_scene_hidden": self.cleared_scene_hidden,
            "size_before": self.size_before,
            "size_after": self.size_after,
            "saved": self.saved,
            "errors": self.errors,
        }


def repair_model(
    src: str | Path,
    dest: str | Path,
    operations: list[str],
    progress: Callable[[str, float], None] | None = None,
) -> RepairReport:
    """Применить выбранные операции и пересохранить модель."""
    src, dest = Path(src), Path(dest)
    ordered = [op for op in OPERATION_ORDER if op in operations]
    report = RepairReport(operations=ordered, size_before=src.stat().st_size)

    if not ordered:
        report.errors.append("не выбрано ни одной операции")
        return report

    lib = load_sdk()
    _ensure_initialized(lib)

    # Первый заход — не трогая сцены. На большинстве файлов этого хватает.
    if not _attempt(lib, src, dest, report, ordered, False, progress):
        # Не сохранилось: у сцен повисли ссылки на скрытые объекты. Пройти
        # по этому списку ПОСЛЕ чистки нельзя — там битые указатели, и
        # обращение к ним роняет процесс, а не возвращает ошибку. Поэтому
        # начинаем заново и расчищаем сцены заранее.
        if progress:
            progress("сцены мешают сохранению, захожу заново", 0.45)
        _reset_counters(report)
        if not _attempt(lib, src, dest, report, ordered, True, progress):
            report.errors.append("модель не сохранилась даже после расчистки сцен")

    if dest.exists():
        report.size_after = dest.stat().st_size
    elif not report.errors:
        report.errors.append("SDK не создал выходной файл")

    if progress:
        progress("готово", 1.0)
    return report


def _reset_counters(report: RepairReport) -> None:
    report.removed_definitions = 0
    report.passes = 0
    report.erased_edges = 0
    report.skipped_linework = 0
    report.textures_seen = 0
    report.textures_resized = 0
    report.textures_failed = 0


def _attempt(
    lib,
    src: Path,
    dest: Path,
    report: RepairReport,
    operations: list[str],
    clear_hidden: bool,
    progress: Callable[[str, float], None] | None,
) -> bool:
    model = SUModelRef()
    call(lib, "SUModelCreateFromFile", byref(model), str(src).encode("utf-8"))
    try:
        report.definitions_before = _safe_count(
            lib, "SUModelGetNumComponentDefinitions", model
        )
        report.scenes = _safe_count(lib, "SUModelGetNumScenes", model)

        if clear_hidden:
            report.cleared_scene_hidden = _clear_scene_hidden(lib, model, report)

        for index, op in enumerate(operations):
            base = 0.1 + 0.6 * index / max(len(operations), 1)
            if op == "fix_errors":
                if progress:
                    progress("чиню внутренние ошибки модели", base)
                call(lib, "SUModelFixErrors", model)
                report.fix_errors_ran = True
            elif op == "erase_loose_edges":
                if progress:
                    progress("убираю висячие рёбра", base)
                report.erased_edges = _erase_loose_edges(lib, model, report)
            elif op == "normalize_textures":
                if progress:
                    progress("привожу текстуры к степени двойки", base)
                _normalize_textures(lib, model, report, progress)
            elif op == "purge_unused":
                if progress:
                    progress("выбрасываю неиспользуемое", base)
                _purge_definitions(lib, model, report, progress)

        report.definitions_after = _safe_count(
            lib, "SUModelGetNumComponentDefinitions", model
        )

        if progress:
            progress("сохраняю модель", 0.9 if clear_hidden else 0.4)
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


# --------------------------------------------------------------------------
# Обход модели
# --------------------------------------------------------------------------


def _all_entities(lib, model) -> list:
    """Все контейнеры сущностей: корень, группы и определения компонентов."""
    containers = []
    root = SUEntitiesRef()
    call(lib, "SUModelGetEntities", model, byref(root))

    seen: set[int] = set()
    stack = [root]
    while stack:
        entities = stack.pop()
        key = entities.ptr or 0
        if key in seen:
            continue
        seen.add(key)
        containers.append(entities)

        group_count = _safe_count(lib, "SUEntitiesGetNumGroups", entities)
        for group in get_array(
            lib, "SUEntitiesGetGroups", entities, group_count, SUGroupRef
        ):
            sub = SUEntitiesRef()
            try:
                call(lib, "SUGroupGetEntities", group, byref(sub))
                stack.append(sub)
            except SdkError:
                pass

    count = _safe_count(lib, "SUModelGetNumComponentDefinitions", model)
    for definition in get_array(
        lib, "SUModelGetComponentDefinitions", model, count, SUComponentDefinitionRef
    ):
        sub = SUEntitiesRef()
        try:
            call(lib, "SUComponentDefinitionGetEntities", definition, byref(sub))
        except SdkError:
            continue
        if (sub.ptr or 0) not in seen:
            seen.add(sub.ptr or 0)
            containers.append(sub)

    return containers


# --------------------------------------------------------------------------
# Операции
# --------------------------------------------------------------------------


def _erase_loose_edges(lib, model, report: RepairReport) -> int:
    """Удалить рёбра, не принадлежащие ни одной грани.

    Это следы построения и остатки импорта: на вид ничего, а при выделении
    рамкой цепляется всё подряд.

    Важная оговорка: контейнеры без единой грани не трогаем. Компонент,
    состоящий из одних линий, — это обычно осмысленное содержимое: 2D-подложка,
    план, разметка, контур логотипа. Стереть в нём висячие рёбра значит
    стереть его целиком.
    """
    erased = 0
    for entities in _all_entities(lib, model):
        count = _safe_count(lib, "SUEntitiesGetNumEdges", entities, True)
        if not count:
            continue
        if _safe_count(lib, "SUEntitiesGetNumFaces", entities) == 0:
            report.skipped_linework += 1
            continue
        try:
            edges = get_array(
                lib, "SUEntitiesGetEdges", entities, count, SUEdgeRef, True
            )
        except SdkError as exc:
            report.errors.append(f"не прочитались рёбра: {exc}")
            continue

        loose = []
        for edge in edges:
            try:
                if _safe_count(lib, "SUEdgeGetNumFaces", edge) == 0:
                    loose.append(SUEntityRef(edge.ptr))
            except SdkError:
                continue
        if not loose:
            continue
        try:
            array = (SUEntityRef * len(loose))(*loose)
            call(lib, "SUEntitiesErase", entities, len(loose), array)
            erased += len(loose)
        except SdkError as exc:
            report.errors.append(f"не удалились висячие рёбра: {exc}")
    return erased


def _reciprocal(value: float) -> float:
    """Обратная величина с защитой от нуля."""
    return 1.0 / value if abs(value) > 1e-12 else 1.0


def _next_pot(value: int, limit: int) -> int:
    """Ближайшая степень двойки, не больше исходника и не больше предела."""
    if value < 1:
        return 1
    pot = 1 << (value.bit_length() - 1)
    return max(1, min(pot, limit))


def _normalize_textures(lib, model, report: RepairReport, progress) -> None:
    """Привести текстуры к степени двойки, сохранив привязку к модели.

    Пересоздавать текстуру приходится через временный файл: у
    ``SUTextureCreateFromImageRep`` нет параметров масштаба, и текстура
    теряет свой размер в модели. ``SUTextureCreateFromFile`` масштаб
    принимает, поэтому s_scale и t_scale переносятся как есть.
    """
    count = _safe_count(lib, "SUModelGetNumMaterials", model)
    materials = get_array(lib, "SUModelGetMaterials", model, count, SUMaterialRef)

    with tempfile.TemporaryDirectory(prefix="atata-tex-") as tmp:
        tmpdir = Path(tmp)
        for index, material in enumerate(materials):
            if progress and index % 50 == 0:
                progress(
                    f"текстуры: {index}/{len(materials)}",
                    0.4 + 0.3 * index / max(len(materials), 1),
                )
            try:
                _normalize_one(lib, material, tmpdir, index, report)
            except SdkError as exc:
                report.textures_failed += 1
                if len(report.errors) < 20:
                    report.errors.append(f"текстура не переделана: {exc}")


def _normalize_one(lib, material, tmpdir: Path, index: int, report: RepairReport) -> None:
    texture = SUTextureRef()
    try:
        call(lib, "SUMaterialGetTexture", material, byref(texture))
    except SdkError:
        return  # у материала нет текстуры — это норма

    report.textures_seen += 1

    width, height = c_size_t(), c_size_t()
    s_scale, t_scale = c_double(), c_double()
    call(
        lib,
        "SUTextureGetDimensions",
        texture,
        byref(width),
        byref(height),
        byref(s_scale),
        byref(t_scale),
    )

    target_w = _next_pot(width.value, MAX_TEXTURE_DIM)
    target_h = _next_pot(height.value, MAX_TEXTURE_DIM)
    if (target_w, target_h) == (width.value, height.value):
        return

    alpha = c_bool()
    try:
        call(lib, "SUTextureGetUseAlphaChannel", texture, byref(alpha))
    except SdkError:
        alpha.value = False

    try:
        name = read_string(lib, "SUTextureGetFileName", texture)
    except SdkError:
        name = ""

    # Объект картинки нужно создать заранее: SUTextureGetImageRep только
    # заполняет уже существующий, иначе возвращает SU_ERROR_INVALID_OUTPUT.
    image = SUImageRepRef()
    call(lib, "SUImageRepCreate", byref(image))
    call(lib, "SUTextureGetImageRep", texture, byref(image))
    try:
        call(lib, "SUImageRepResize", image, target_w, target_h)
        # PNG когда есть прозрачность, иначе JPEG: задача — снять нагрузку
        # на видеопамять, а не раздуть файл несжатой картинкой.
        suffix = ".png" if alpha.value else ".jpg"
        path = tmpdir / f"tex_{index}{suffix}"
        call(lib, "SUImageRepSaveToFile", image, str(path).encode("utf-8"))
    finally:
        try:
            call(lib, "SUImageRepRelease", byref(image))
        except SdkError:
            pass

    if not path.exists():
        report.textures_failed += 1
        return

    # SUTextureGetDimensions и SUTextureCreateFromFile пользуются обратными
    # соглашениями о масштабе: измерено на реальном файле — 0.1 на чтении
    # соответствует 10.0 на записи, 0.254 -> 3.937, 0.0508 -> 19.685.
    # Поэтому передаём обратные величины, иначе плитка текстуры меняет
    # размер в модели. Разрешение картинки в эту связь не входит.
    replacement = SUTextureRef()
    call(
        lib,
        "SUTextureCreateFromFile",
        byref(replacement),
        str(path).encode("utf-8"),
        _reciprocal(s_scale.value),
        _reciprocal(t_scale.value),
    )
    if name:
        try:
            call(lib, "SUTextureSetFileName", replacement, name.encode("utf-8"))
        except SdkError:
            pass

    call(lib, "SUMaterialSetTexture", material, replacement)
    report.textures_resized += 1

    # Проверяем инвариант: привязка текстуры к размерам в модели не изменилась.
    check_w, check_h = c_size_t(), c_size_t()
    check_s, check_t = c_double(), c_double()
    try:
        applied = SUTextureRef()
        call(lib, "SUMaterialGetTexture", material, byref(applied))
        call(
            lib,
            "SUTextureGetDimensions",
            applied,
            byref(check_w),
            byref(check_h),
            byref(check_s),
            byref(check_t),
        )
        if abs(check_s.value - s_scale.value) > 1e-6 or abs(
            check_t.value - t_scale.value
        ) > 1e-6:
            report.texture_scale_kept = False
    except SdkError:
        pass


def _purge_definitions(lib, model, report: RepairReport, progress) -> None:
    for attempt in range(MAX_PASSES):
        layer = _removable(lib, model)
        if not layer:
            break
        if progress:
            progress(f"чищу неиспользуемое, проход {attempt + 1}", 0.75)
        array = (SUComponentDefinitionRef * len(layer))(*layer)
        call(lib, "SUModelRemoveComponentDefinitions", model, len(layer), array)
        report.removed_definitions += len(layer)
        report.passes = attempt + 1
    else:
        report.errors.append(
            f"чистка не сошлась за {MAX_PASSES} проходов — можно прогнать ещё раз"
        )

    if PURGE_LAYERS:
        try:
            value = c_size_t()
            call(lib, "SUModelPurgeUnusedLayers", model, byref(value))
            report.removed_layers = value.value
        except SdkError as exc:
            report.errors.append(f"чистка тегов: {exc}")


def _removable(lib, model) -> list:
    """Определения, которые можно снять на текущем проходе.

    Берём только те, у которых нет вставок ВООБЩЕ. Определение с
    ``used_instances == 0``, но ``instances > 0`` лежит внутри другого
    неиспользуемого; снеся оба разом, освободим вложенное, пока родитель
    на него ещё ссылается. Поэтому чистим слоями.
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


def _clear_scene_hidden(lib, model, report: RepairReport) -> int:
    """Снять со сцен пометки «скрыто».

    Только ДО изменения модели: после в списке остаются висячие указатели,
    и обращение к ним роняет процесс access violation-ом.
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
            # Все SU*Ref — это struct { void* ptr }, приведение типа сводится
            # к переносу указателя; штатные From/To объявлены inline и из DLL
            # не экспортируются.
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
