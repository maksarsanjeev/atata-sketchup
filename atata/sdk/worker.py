"""Отдельный процесс разбора геометрии через SketchUp SDK.

Запускается как подпроцесс и отдаёт результат JSON-ом::

    python -m atata.sdk.worker inspect model.skp result.json
    python -m atata.sdk.worker check   model.skp result.json
    python -m atata.sdk.worker repair  model.skp clean.skp fix_errors,purge_unused result.json

Без имени файла результат уходит в stdout — годится для отладки руками,
но не для разбора вызывающей стороной (см. :func:`_emit`).

Вынесен в отдельный процесс по трём причинам:

* SDK живёт только под Windows и macOS, а веб-сервис — под Linux;
  граница процесса позволяет запустить его через Wine или на другой машине;
* модель разворачивается в памяти в разы больше файла (замер: 4.5 ГБ на
  файле 348 МБ), и по завершении процесса память возвращается ОС сразу,
  а не остаётся в аллокаторе питона;
* падение SDK на кривом файле не утаскивает за собой веб-сервис.

Зависимостей, кроме стандартной библиотеки, у модуля нет — под Wine хватает
embeddable-сборки Windows-питона.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .capi import SdkError, SdkUnavailable, sdk_status
from .inspect import inspect_model


def to_native_path(path: str) -> str:
    """Под Wine unix-путь нужно превратить в путь на диске Z:.

    Wine отображает корень файловой системы на Z:, но Windows-программа
    получает ``/home/...`` как путь без буквы диска и ищет его на текущем
    диске. Явный префикс снимает неоднозначность.
    """
    if path.startswith("/") and sys.platform == "win32":
        return "Z:" + path.replace("/", "\\")
    return path


def _emit(payload: dict, out_path: str | None) -> None:
    """Отдать результат.

    Через stdout JSON приходит покорёженным: и PowerShell при
    перенаправлении, и консоль Wine норовят переломать длинную строку и
    переколбасить кодировку. Поэтому основной режим — запись в файл,
    а stdout остаётся для отладки руками.
    """
    text = json.dumps(payload, ensure_ascii=False)
    if out_path:
        Path(out_path).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        _emit({"ok": False, "error": "не передана команда"}, None)
        return 2

    command = argv[0]
    if command == "repair":
        return _repair(argv[1:])
    if command == "inspect":
        return _inspect(argv[1:])
    if command == "check":
        return _check(argv[1:])
    _emit({"ok": False, "error": f"неизвестная команда: {command}"}, None)
    return 2


def _repair(argv: list[str]) -> int:
    """``repair SRC DEST op1,op2 OUT.json``"""
    if len(argv) < 3:
        _emit(
            {"ok": False, "error": "нужны: исходный .skp, выходной .skp, список операций"},
            None,
        )
        return 2

    from .repair import repair_model

    src = to_native_path(argv[0])
    dest = to_native_path(argv[1])
    operations = [op for op in argv[2].split(",") if op]
    out_path = to_native_path(argv[3]) if len(argv) > 3 else None

    try:
        report = repair_model(src, dest, operations)
    except SdkUnavailable as exc:
        _emit({"ok": False, "error": str(exc), "kind": "unavailable"}, out_path)
        return 3
    except SdkError as exc:
        _emit({"ok": False, "error": str(exc), "kind": "sdk"}, out_path)
        return 4
    except Exception as exc:  # noqa: BLE001
        _emit(
            {"ok": False, "error": f"{type(exc).__name__}: {exc}", "kind": "other"},
            out_path,
        )
        return 5

    _emit({"ok": True, "repair": report.as_dict()}, out_path)
    return 0


def _check(argv: list[str]) -> int:
    """Открывается ли файл настоящим читателем SketchUp.

    Единственная честная проверка результата. Целый ZIP-контейнер ничего
    не гарантирует: файл может пройти проверку архива и всё равно не
    открыться в SketchUp.
    """
    if not argv:
        _emit({"ok": False, "error": "не передан путь к .skp"}, None)
        return 2

    from ctypes import byref

    from .capi import SUModelRef, call, load_sdk
    from .inspect import _ensure_initialized

    out_path = to_native_path(argv[1]) if len(argv) > 1 else None
    target = to_native_path(argv[0])

    try:
        lib = load_sdk()
        _ensure_initialized(lib)
        model = SUModelRef()
        call(lib, "SUModelCreateFromFile", byref(model), target.encode("utf-8"))
        try:
            call(lib, "SUModelRelease", byref(model))
        except SdkError:
            pass
    except SdkUnavailable as exc:
        _emit({"ok": False, "error": str(exc), "kind": "unavailable"}, out_path)
        return 3
    except SdkError as exc:
        _emit({"ok": False, "error": str(exc), "kind": "sdk"}, out_path)
        return 4

    _emit({"ok": True, "openable": True}, out_path)
    return 0


def _inspect(argv: list[str]) -> int:
    if not argv:
        _emit({"ok": False, "error": "не передан путь к .skp"}, None)
        return 2

    raw = argv[0]
    out_path = to_native_path(argv[1]) if len(argv) > 1 else None
    target = to_native_path(raw)

    try:
        facts = inspect_model(target)
    except SdkUnavailable as exc:
        _emit({"ok": False, "error": str(exc), "kind": "unavailable"}, out_path)
        return 3
    except SdkError as exc:
        _emit({"ok": False, "error": str(exc), "kind": "sdk"}, out_path)
        return 4
    except Exception as exc:  # noqa: BLE001 — наружу должен уйти JSON, не трейс
        _emit(
            {"ok": False, "error": f"{type(exc).__name__}: {exc}", "kind": "other"},
            out_path,
        )
        return 5

    status = sdk_status()
    payload = facts.as_dict()
    payload["definitions_detail"] = [
        {
            "name": d.name,
            "own_faces": d.own_faces,
            "expanded_faces": d.expanded_faces,
            "instances": d.instances,
            "used_instances": d.used_instances,
        }
        for d in facts.definitions
    ]
    payload["materials_list"] = facts.materials
    payload["layers_list"] = facts.layers

    _emit(
        {
            "ok": True,
            "path": str(Path(raw)),
            "sdk": {"library": status.library, "api_version": status.version},
            "model": payload,
        },
        out_path,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
