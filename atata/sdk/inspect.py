"""Обход модели через SDK: то, что лежит в model.dat.

Считает не «сколько граней лежит в файле», а сколько их реально в сцене:
определение компонента, вставленное сорок раз, даёт сорок наборов геометрии.
Поэтому определения раскрываются рекурсивно с мемоизацией по указателю.
"""

from __future__ import annotations

import atexit
import ctypes
from ctypes import byref, c_size_t
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .capi import (
    SUComponentDefinitionRef,
    SUComponentInstanceRef,
    SUEntitiesRef,
    SUGroupRef,
    SULayerRef,
    SUMaterialRef,
    SUModelRef,
    SUStylesRef,
    SdkError,
    call,
    get_array,
    get_count,
    load_sdk,
    read_string,
)

MAX_DEPTH = 64


@dataclass
class DefinitionInfo:
    name: str
    own_faces: int
    own_edges: int
    expanded_faces: int
    instances: int
    used_instances: int

    @property
    def total_faces(self) -> int:
        """Вклад определения в сцену с учётом всех его вставок."""
        return self.expanded_faces * max(self.used_instances, 0)


@dataclass
class ModelFacts:
    path: str
    root_faces: int = 0
    root_edges: int = 0
    total_faces: int = 0
    total_edges: int = 0
    loose_edges: int = 0
    definitions: list[DefinitionInfo] = field(default_factory=list)
    materials: list[str] = field(default_factory=list)
    layers: list[str] = field(default_factory=list)
    scenes: int = 0
    styles: int = 0
    max_depth: int = 0
    truncated: bool = False

    @property
    def unused_definitions(self) -> list[DefinitionInfo]:
        return [d for d in self.definitions if d.used_instances == 0]

    def heaviest(self, limit: int = 20) -> list[DefinitionInfo]:
        return sorted(self.definitions, key=lambda d: -d.total_faces)[:limit]

    def as_dict(self) -> dict:
        return {
            "root_faces": self.root_faces,
            "root_edges": self.root_edges,
            "total_faces": self.total_faces,
            "total_edges": self.total_edges,
            "loose_edges": self.loose_edges,
            "definitions": len(self.definitions),
            "unused_definitions": len(self.unused_definitions),
            "materials": len(self.materials),
            "layers": len(self.layers),
            "scenes": self.scenes,
            "styles": self.styles,
            "max_depth": self.max_depth,
            "truncated": self.truncated,
            "heaviest": [
                {
                    "name": d.name,
                    "faces": d.total_faces,
                    "own_faces": d.own_faces,
                    "instances": d.used_instances,
                }
                for d in self.heaviest()
            ],
        }


_initialized = False


def _ensure_initialized(lib) -> None:
    """SUInitialize зовётся один раз на процесс.

    Повторная пара Terminate/Initialize в одном процессе поведение SDK
    не гарантирует, поэтому терминируем только на выходе.
    """
    global _initialized
    if _initialized:
        return
    call(lib, "SUInitialize")
    _initialized = True

    def _shutdown() -> None:
        try:
            lib.SUTerminate()
        except Exception:
            pass

    atexit.register(_shutdown)


def inspect_model(
    path: str | Path,
    progress: Callable[[str, float], None] | None = None,
) -> ModelFacts:
    """Открыть .skp через SDK и посчитать содержимое model.dat."""
    lib = load_sdk()
    _ensure_initialized(lib)

    path = Path(path)
    facts = ModelFacts(path=str(path))

    model = SUModelRef()
    call(lib, "SUModelCreateFromFile", byref(model), str(path).encode("utf-8"))
    try:
        if progress:
            progress("читаю дерево модели", 0.1)

        root = SUEntitiesRef()
        call(lib, "SUModelGetEntities", model, byref(root))

        memo: dict[int, tuple[int, int]] = {}
        state = {"truncated": False, "max_depth": 0}

        facts.root_faces, facts.root_edges = _walk(lib, root, memo, set(), 1, state)
        facts.loose_edges = get_count(lib, "SUEntitiesGetNumEdges", root, True)

        if progress:
            progress("считаю компоненты", 0.5)

        facts.definitions = _definitions(lib, model, memo, state)

        # Суммарная геометрия сцены: корень плюс вклад каждого определения.
        facts.total_faces = facts.root_faces
        facts.total_edges = facts.root_edges

        if progress:
            progress("собираю материалы и слои", 0.8)

        facts.materials = _named(lib, model, "SUModelGetNumMaterials",
                                 "SUModelGetMaterials", SUMaterialRef, "SUMaterialGetName")
        facts.layers = _named(lib, model, "SUModelGetNumLayers",
                              "SUModelGetLayers", SULayerRef, "SULayerGetName")
        facts.scenes = _safe_count(lib, "SUModelGetNumScenes", model)
        facts.styles = _style_count(lib, model)

        facts.max_depth = state["max_depth"]
        facts.truncated = state["truncated"]

        if progress:
            progress("готово", 1.0)
        return facts
    finally:
        try:
            call(lib, "SUModelRelease", byref(model))
        except SdkError:
            pass


def _walk(lib, entities, memo, path_guard, depth, state) -> tuple[int, int]:
    """Посчитать грани и рёбра набора сущностей, раскрывая вложенное."""
    if depth > MAX_DEPTH:
        state["truncated"] = True
        return 0, 0
    state["max_depth"] = max(state["max_depth"], depth)

    faces = get_count(lib, "SUEntitiesGetNumFaces", entities)
    edges = get_count(lib, "SUEntitiesGetNumEdges", entities, False)

    # Группы — это тоже определения, но вставленные ровно один раз,
    # поэтому раскрываем их на месте и не мемоизируем.
    group_count = get_count(lib, "SUEntitiesGetNumGroups", entities)
    for group in get_array(lib, "SUEntitiesGetGroups", entities, group_count, SUGroupRef):
        sub = SUEntitiesRef()
        call(lib, "SUGroupGetEntities", group, byref(sub))
        f, e = _walk(lib, sub, memo, path_guard, depth + 1, state)
        faces += f
        edges += e

    instance_count = get_count(lib, "SUEntitiesGetNumInstances", entities)
    for instance in get_array(
        lib, "SUEntitiesGetInstances", entities, instance_count, SUComponentInstanceRef
    ):
        definition = SUComponentDefinitionRef()
        call(lib, "SUComponentInstanceGetDefinition", instance, byref(definition))
        f, e = _expand(lib, definition, memo, path_guard, depth + 1, state)
        faces += f
        edges += e

    return faces, edges


def _expand(lib, definition, memo, path_guard, depth, state) -> tuple[int, int]:
    """Геометрия одного определения со всем вложенным, с мемоизацией."""
    key = definition.ptr or 0
    if key in memo:
        return memo[key]
    if key in path_guard:
        # Циклическая вставка — в корректной модели невозможна, но защита
        # от бесконечной рекурсии стоит дешевле разбора аварийного дампа.
        state["truncated"] = True
        return 0, 0

    path_guard.add(key)
    try:
        entities = SUEntitiesRef()
        call(lib, "SUComponentDefinitionGetEntities", definition, byref(entities))
        result = _walk(lib, entities, memo, path_guard, depth, state)
    finally:
        path_guard.discard(key)

    memo[key] = result
    return result


def _definitions(lib, model, memo, state) -> list[DefinitionInfo]:
    count = _safe_count(lib, "SUModelGetNumComponentDefinitions", model)
    refs = get_array(
        lib,
        "SUModelGetComponentDefinitions",
        model,
        count,
        SUComponentDefinitionRef,
    )

    out: list[DefinitionInfo] = []
    for ref in refs:
        try:
            name = read_string(lib, "SUComponentDefinitionGetName", ref)
        except SdkError:
            name = "<без имени>"

        entities = SUEntitiesRef()
        try:
            call(lib, "SUComponentDefinitionGetEntities", ref, byref(entities))
            own_faces = get_count(lib, "SUEntitiesGetNumFaces", entities)
            own_edges = get_count(lib, "SUEntitiesGetNumEdges", entities, False)
        except SdkError:
            own_faces = own_edges = 0

        expanded_faces, _ = _expand(lib, ref, memo, set(), 1, state)

        out.append(
            DefinitionInfo(
                name=name,
                own_faces=own_faces,
                own_edges=own_edges,
                expanded_faces=expanded_faces,
                instances=_safe_count(lib, "SUComponentDefinitionGetNumInstances", ref),
                used_instances=_safe_count(
                    lib, "SUComponentDefinitionGetNumUsedInstances", ref
                ),
            )
        )
    return out


def _named(lib, model, count_getter, list_getter, item_type, name_getter) -> list[str]:
    count = _safe_count(lib, count_getter, model)
    refs = get_array(lib, list_getter, model, count, item_type)
    names = []
    for ref in refs:
        try:
            names.append(read_string(lib, name_getter, ref))
        except SdkError:
            names.append("<без имени>")
    return names


def _style_count(lib, model) -> int:
    """У модели нет SUModelGetNumStyles: сперва коллекция, потом счётчик."""
    try:
        styles = SUStylesRef()
        call(lib, "SUModelGetStyles", model, byref(styles))
        return get_count(lib, "SUStylesGetNumStyles", styles)
    except SdkError:
        return 0


def _safe_count(lib, getter: str, ref, *extra) -> int:
    """Часть счётчиков есть не во всех версиях SDK — их отсутствие не фатально."""
    try:
        return get_count(lib, getter, ref, *extra)
    except SdkError:
        return 0
