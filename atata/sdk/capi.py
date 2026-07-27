"""ctypes-обвязка над SketchUp C API.

C API — плоский C-интерфейс: все типы это структуры с единственным
указателем, все функции возвращают код ``SUResult``. Компилировать ничего
не нужно, хватает ctypes.

Платформы: SDK существует под **Windows x64** (``SketchUpAPI.dll``) и
**macOS** (``SketchUpAPI.framework``). Под Linux сборки нет — на Linux
загрузка честно падает с :class:`SdkUnavailable`.

Сигнатуры объявлены по публичной документации C API. Каждая функция
объявляется лениво: если в конкретной версии SDK её нет, ошибка вылезет
на вызове с внятным именем, а не при импорте модуля.
"""

from __future__ import annotations

import ctypes
import os
import platform
import sys
from ctypes import POINTER, byref, c_bool, c_char_p, c_size_t, c_void_p
from dataclasses import dataclass
from pathlib import Path

SU_ERROR_NONE = 0

# enum SUComponentType
SU_COMPONENT_NORMAL = 0
SU_COMPONENT_GROUP = 1
SU_COMPONENT_IMAGE = 2
COMPONENT_TYPE_NAMES = {0: "обычный компонент", 1: "группа", 2: "изображение"}

# Расшифровки кодов возврата, которые реально встречаются при чтении файла.
SU_RESULTS = {
    0: "SU_ERROR_NONE",
    1: "SU_ERROR_NULL_POINTER_INPUT",
    2: "SU_ERROR_INVALID_INPUT",
    3: "SU_ERROR_NULL_POINTER_OUTPUT",
    4: "SU_ERROR_INVALID_OUTPUT",
    5: "SU_ERROR_OVERWRITE_VALID",
    6: "SU_ERROR_GENERIC",
    7: "SU_ERROR_SERIALIZATION",
    8: "SU_ERROR_OUT_OF_RANGE",
    9: "SU_ERROR_NO_DATA",
    10: "SU_ERROR_INSUFFICIENT_SIZE",
    11: "SU_ERROR_UNKNOWN_EXCEPTION",
    12: "SU_ERROR_MODEL_INVALID",
    13: "SU_ERROR_MODEL_VERSION",
    14: "SU_ERROR_LAYER_LOCKED",
    15: "SU_ERROR_DUPLICATE",
    16: "SU_ERROR_PARTIAL_SUCCESS",
    17: "SU_ERROR_UNSUPPORTED",
    18: "SU_ERROR_INVALID_ARGUMENT",
    19: "SU_ERROR_ENTITY_LOCKED",
    20: "SU_ERROR_INVALID_OPERATION",
}


class SdkError(RuntimeError):
    """SDK вернул код ошибки."""


class SdkUnavailable(RuntimeError):
    """Библиотека SDK не найдена или не поддерживается на этой платформе."""


# --------------------------------------------------------------------------
# Типы C API: каждый — структура с одним указателем
# --------------------------------------------------------------------------


def _ref_type(name: str):
    return type(name, (ctypes.Structure,), {"_fields_": [("ptr", c_void_p)]})


SUModelRef = _ref_type("SUModelRef")
SUEntitiesRef = _ref_type("SUEntitiesRef")
SUComponentDefinitionRef = _ref_type("SUComponentDefinitionRef")
SUComponentInstanceRef = _ref_type("SUComponentInstanceRef")
SUGroupRef = _ref_type("SUGroupRef")
SUMaterialRef = _ref_type("SUMaterialRef")
SULayerRef = _ref_type("SULayerRef")
SUStringRef = _ref_type("SUStringRef")
SUStylesRef = _ref_type("SUStylesRef")
SUSceneRef = _ref_type("SUSceneRef")
SUEntityRef = _ref_type("SUEntityRef")
SUDrawingElementRef = _ref_type("SUDrawingElementRef")
SUEdgeRef = _ref_type("SUEdgeRef")
SUTextureRef = _ref_type("SUTextureRef")
SUImageRepRef = _ref_type("SUImageRepRef")

# Эти три функции объявлены в заголовках как void, а не SU_RESULT.
# Проверять у них код возврата нельзя: в регистре будет мусор, и любая
# инициализация падала бы с выдуманной ошибкой.
VOID_FUNCTIONS = {"SUInitialize", "SUTerminate", "SUGetAPIVersion"}


@dataclass
class SdkStatus:
    available: bool
    library: str | None = None
    version: str | None = None
    reason: str | None = None

    def as_dict(self) -> dict:
        return {
            "available": self.available,
            "library": self.library,
            "version": self.version,
            "reason": self.reason,
        }


# --------------------------------------------------------------------------
# Поиск библиотеки
# --------------------------------------------------------------------------


def _candidates() -> list[Path]:
    """Где искать библиотеку SDK, в порядке приоритета."""
    found: list[Path] = []

    env = os.environ.get("ATATA_SDK_PATH")
    system = platform.system()

    if system == "Windows":
        leaf = Path("binaries/sketchup/x64/SketchUpAPI.dll")
        names = ["SketchUpAPI.dll"]
    elif system == "Darwin":
        leaf = Path("binaries/sketchup/x64/SketchUpAPI.framework/SketchUpAPI")
        names = ["SketchUpAPI.framework/SketchUpAPI", "libSketchUpAPI.dylib"]
    else:
        return []

    if env:
        root = Path(env)
        # Можно указать как каталог распакованного SDK, так и файл напрямую.
        if root.is_file():
            found.append(root)
        else:
            found.append(root / leaf)
            found.extend(root / name for name in names)

    for root in (Path.cwd() / "sdk", Path(sys.prefix) / "sdk"):
        found.append(root / leaf)

    return [p for p in found if p.exists()]


_loaded: ctypes.CDLL | None = None
_status: SdkStatus | None = None


def load_sdk() -> ctypes.CDLL:
    """Загрузить библиотеку SDK. Кэшируется на процесс."""
    global _loaded, _status

    if _loaded is not None:
        return _loaded

    system = platform.system()
    if system not in ("Windows", "Darwin"):
        _status = SdkStatus(
            available=False,
            reason=(
                f"SketchUp SDK не существует под {system}: Trimble выпускает его "
                f"только под Windows x64 и macOS. Разбор геометрии нужно выносить "
                f"в воркер на поддерживаемой платформе."
            ),
        )
        raise SdkUnavailable(_status.reason)

    paths = _candidates()
    if not paths:
        _status = SdkStatus(
            available=False,
            reason=(
                "библиотека SDK не найдена. Распакуйте SketchUp SDK и укажите "
                "путь в ATATA_SDK_PATH — см. README."
            ),
        )
        raise SdkUnavailable(_status.reason)

    path = paths[0]
    try:
        if system == "Windows":
            # Соседние DLL (SketchUpCommonPreferences.dll) грузятся по путям
            # поиска, поэтому каталог надо добавить явно.
            os.add_dll_directory(str(path.parent))
            lib = ctypes.WinDLL(str(path))
        else:
            lib = ctypes.CDLL(str(path))
    except OSError as exc:
        _status = SdkStatus(available=False, reason=f"не загрузилась {path}: {exc}")
        raise SdkUnavailable(_status.reason) from exc

    _declare(lib)
    _loaded = lib
    _status = SdkStatus(available=True, library=str(path), version=_read_version(lib))
    return lib


def sdk_status() -> SdkStatus:
    """Состояние SDK без выброса исключения — для /health и интерфейса."""
    if _status is not None:
        return _status
    try:
        load_sdk()
    except (SdkUnavailable, SdkError) as exc:
        if _status is None:
            return SdkStatus(available=False, reason=str(exc))
    return _status or SdkStatus(available=False, reason="неизвестно")


def _read_version(lib: ctypes.CDLL) -> str | None:
    major, minor = ctypes.c_size_t(), ctypes.c_size_t()
    try:
        lib.SUGetAPIVersion(byref(major), byref(minor))
    except Exception:
        return None
    return f"{major.value}.{minor.value}"


# --------------------------------------------------------------------------
# Объявления сигнатур
# --------------------------------------------------------------------------

# (имя, [типы аргументов]) — возвращают все SUResult (c_int), кроме отмеченных.
_SIGNATURES: list[tuple[str, list]] = [
    ("SUInitialize", []),
    ("SUTerminate", []),
    ("SUGetAPIVersion", [POINTER(c_size_t), POINTER(c_size_t)]),
    # модель
    ("SUModelCreateFromFile", [POINTER(SUModelRef), c_char_p]),
    ("SUModelRelease", [POINTER(SUModelRef)]),
    ("SUModelGetEntities", [SUModelRef, POINTER(SUEntitiesRef)]),
    ("SUModelGetNumMaterials", [SUModelRef, POINTER(c_size_t)]),
    ("SUModelGetMaterials", [SUModelRef, c_size_t, POINTER(SUMaterialRef), POINTER(c_size_t)]),
    ("SUModelGetNumComponentDefinitions", [SUModelRef, POINTER(c_size_t)]),
    ("SUModelGetComponentDefinitions", [SUModelRef, c_size_t, POINTER(SUComponentDefinitionRef), POINTER(c_size_t)]),
    ("SUModelGetNumLayers", [SUModelRef, POINTER(c_size_t)]),
    ("SUModelGetLayers", [SUModelRef, c_size_t, POINTER(SULayerRef), POINTER(c_size_t)]),
    ("SUModelGetNumScenes", [SUModelRef, POINTER(c_size_t)]),
    # Числа стилей у модели нет: сначала берётся коллекция, потом счётчик.
    ("SUModelGetStyles", [SUModelRef, POINTER(SUStylesRef)]),
    ("SUStylesGetNumStyles", [SUStylesRef, POINTER(c_size_t)]),
    # запись и чистка
    ("SUModelSaveToFile", [SUModelRef, c_char_p]),
    (
        "SUModelRemoveComponentDefinitions",
        [SUModelRef, c_size_t, POINTER(SUComponentDefinitionRef)],
    ),
    ("SUModelPurgeUnusedLayers", [SUModelRef, POINTER(c_size_t)]),
    ("SUModelPurgeEmptyLayerFolders", [SUModelRef, POINTER(c_size_t)]),
    # Сцены держат свой список скрытых объектов. После удаления определений
    # эти ссылки повисают, и сохранение падает с SU_ERROR_SERIALIZATION.
    ("SUModelGetScenes", [SUModelRef, c_size_t, POINTER(SUSceneRef), POINTER(c_size_t)]),
    ("SUSceneGetNumHiddenEntities", [SUSceneRef, POINTER(c_size_t)]),
    (
        "SUSceneGetHiddenEntities",
        [SUSceneRef, c_size_t, POINTER(SUEntityRef), POINTER(c_size_t)],
    ),
    (
        "SUSceneSetDrawingElementHidden",
        [SUSceneRef, SUDrawingElementRef, c_bool],
    ),
    # Штатная починка модели — то же, что «Fix Problems» в самом SketchUp.
    ("SUModelFixErrors", [SUModelRef]),
    # Устойчивый идентификатор объекта и обратный поиск по нему. Нужны,
    # чтобы вернуть сценам пометки «скрыто» после правок: указатели к тому
    # моменту уже недействительны, а идентификаторы переживают всё.
    ("SUEntityGetPersistentID", [SUEntityRef, POINTER(ctypes.c_int64)]),
    (
        "SUModelGetEntitiesByPersistentIDs",
        [SUModelRef, c_size_t, POINTER(ctypes.c_int64), POINTER(SUEntityRef)],
    ),
    # Висячие рёбра.
    ("SUEntitiesErase", [SUEntitiesRef, c_size_t, POINTER(SUEntityRef)]),
    (
        "SUEntitiesGetEdges",
        [SUEntitiesRef, c_bool, c_size_t, POINTER(SUEdgeRef), POINTER(c_size_t)],
    ),
    ("SUEdgeGetNumFaces", [SUEdgeRef, POINTER(c_size_t)]),
    # Текстуры. Пересоздавать их надо через файл: CreateFromImageRep не
    # принимает масштаб, и привязка текстуры к размерам в модели теряется.
    ("SUMaterialGetTexture", [SUMaterialRef, POINTER(SUTextureRef)]),
    ("SUMaterialSetTexture", [SUMaterialRef, SUTextureRef]),
    (
        "SUTextureGetDimensions",
        [
            SUTextureRef,
            POINTER(c_size_t),
            POINTER(c_size_t),
            POINTER(ctypes.c_double),
            POINTER(ctypes.c_double),
        ],
    ),
    ("SUTextureGetImageRep", [SUTextureRef, POINTER(SUImageRepRef)]),
    ("SUTextureGetFileName", [SUTextureRef, POINTER(SUStringRef)]),
    ("SUTextureSetFileName", [SUTextureRef, c_char_p]),
    ("SUTextureGetUseAlphaChannel", [SUTextureRef, POINTER(c_bool)]),
    (
        "SUTextureCreateFromFile",
        [POINTER(SUTextureRef), c_char_p, ctypes.c_double, ctypes.c_double],
    ),
    ("SUTextureRelease", [POINTER(SUTextureRef)]),
    ("SUImageRepCreate", [POINTER(SUImageRepRef)]),
    ("SUImageRepRelease", [POINTER(SUImageRepRef)]),
    ("SUImageRepResize", [SUImageRepRef, c_size_t, c_size_t]),
    (
        "SUImageRepGetPixelDimensions",
        [SUImageRepRef, POINTER(c_size_t), POINTER(c_size_t)],
    ),
    ("SUImageRepSaveToFile", [SUImageRepRef, c_char_p]),
    # сущности
    ("SUEntitiesGetNumFaces", [SUEntitiesRef, POINTER(c_size_t)]),
    ("SUEntitiesGetNumEdges", [SUEntitiesRef, c_bool, POINTER(c_size_t)]),
    ("SUEntitiesGetNumGroups", [SUEntitiesRef, POINTER(c_size_t)]),
    ("SUEntitiesGetGroups", [SUEntitiesRef, c_size_t, POINTER(SUGroupRef), POINTER(c_size_t)]),
    ("SUEntitiesGetNumInstances", [SUEntitiesRef, POINTER(c_size_t)]),
    ("SUEntitiesGetInstances", [SUEntitiesRef, c_size_t, POINTER(SUComponentInstanceRef), POINTER(c_size_t)]),
    # группы и компоненты
    ("SUGroupGetEntities", [SUGroupRef, POINTER(SUEntitiesRef)]),
    ("SUComponentInstanceGetDefinition", [SUComponentInstanceRef, POINTER(SUComponentDefinitionRef)]),
    ("SUComponentDefinitionGetEntities", [SUComponentDefinitionRef, POINTER(SUEntitiesRef)]),
    ("SUComponentDefinitionGetName", [SUComponentDefinitionRef, POINTER(SUStringRef)]),
    ("SUComponentDefinitionGetNumInstances", [SUComponentDefinitionRef, POINTER(c_size_t)]),
    ("SUComponentDefinitionGetNumUsedInstances", [SUComponentDefinitionRef, POINTER(c_size_t)]),
    # Группы и изображения в модели — тоже «определения компонентов», но
    # заводит и удаляет их сам SketchUp. Их надо уметь отличать.
    ("SUComponentDefinitionGetType", [SUComponentDefinitionRef, POINTER(ctypes.c_int)]),
    ("SUComponentDefinitionIsInternal", [SUComponentDefinitionRef, POINTER(c_bool)]),
    # материалы и слои
    ("SUMaterialGetName", [SUMaterialRef, POINTER(SUStringRef)]),
    ("SULayerGetName", [SULayerRef, POINTER(SUStringRef)]),
    # строки
    ("SUStringCreate", [POINTER(SUStringRef)]),
    ("SUStringRelease", [POINTER(SUStringRef)]),
    ("SUStringGetUTF8Length", [SUStringRef, POINTER(c_size_t)]),
    ("SUStringGetUTF8", [SUStringRef, c_size_t, c_char_p, POINTER(c_size_t)]),
]


def _declare(lib: ctypes.CDLL) -> None:
    for name, argtypes in _SIGNATURES:
        try:
            fn = getattr(lib, name)
        except AttributeError:
            # Функции нет в этой версии SDK — обнаружим при вызове.
            continue
        fn.argtypes = argtypes
        fn.restype = None if name in VOID_FUNCTIONS else ctypes.c_int


def check(result: int, where: str) -> None:
    if result != SU_ERROR_NONE:
        name = SU_RESULTS.get(result, f"код {result}")
        raise SdkError(f"{where}: {name}")


SU_ERROR_PARTIAL_SUCCESS = 16


def call(lib: ctypes.CDLL, name: str, *args, allow: tuple[int, ...] = ()) -> None:
    """Вызвать функцию SDK и проверить код возврата.

    ``allow`` — коды, которые в данном месте не считаются ошибкой. Нужно,
    например, для поиска по идентификаторам: если часть объектов удалена,
    SDK честно возвращает SU_ERROR_PARTIAL_SUCCESS, и это ожидаемо.
    """
    try:
        fn = getattr(lib, name)
    except AttributeError as exc:
        raise SdkError(f"{name}: нет в загруженной версии SDK") from exc
    if name in VOID_FUNCTIONS:
        fn(*args)
        return
    result = fn(*args)
    if result in allow:
        return
    check(result, name)


def read_string(lib: ctypes.CDLL, getter: str, ref) -> str:
    """Прочитать строку через SUStringRef с обязательным освобождением."""
    string = SUStringRef()
    call(lib, "SUStringCreate", byref(string))
    try:
        call(lib, getter, ref, byref(string))
        length = c_size_t()
        call(lib, "SUStringGetUTF8Length", string, byref(length))
        buf = ctypes.create_string_buffer(length.value + 1)
        written = c_size_t()
        call(lib, "SUStringGetUTF8", string, length.value + 1, buf, byref(written))
        return buf.value.decode("utf-8", "replace")
    finally:
        try:
            call(lib, "SUStringRelease", byref(string))
        except SdkError:
            pass


def get_array(lib: ctypes.CDLL, getter: str, ref, count: int, item_type, *extra):
    """Обёртка над парой ``SU*GetNum*`` / ``SU*Get*``.

    ``extra`` подставляется между объектом и длиной — так устроен, например,
    ``SUEntitiesGetEdges(entities, standalone_only, len, ...)``.
    """
    if count == 0:
        return []
    buf = (item_type * count)()
    written = c_size_t()
    call(lib, getter, ref, *extra, count, buf, byref(written))
    return list(buf[: written.value])


def get_count(lib: ctypes.CDLL, getter: str, ref, *extra) -> int:
    value = c_size_t()
    call(lib, getter, ref, *extra, byref(value))
    return value.value
