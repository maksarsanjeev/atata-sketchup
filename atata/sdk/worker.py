"""Отдельный процесс разбора геометрии через SketchUp SDK.

Запускается как подпроцесс и отдаёт результат JSON-ом в stdout::

    python -m atata.sdk.worker model.skp

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


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        json.dump({"ok": False, "error": "не передан путь к .skp"}, sys.stdout)
        return 2

    raw = argv[0]
    target = to_native_path(raw)

    try:
        facts = inspect_model(target)
    except SdkUnavailable as exc:
        json.dump(
            {"ok": False, "error": str(exc), "kind": "unavailable"},
            sys.stdout,
            ensure_ascii=False,
        )
        return 3
    except SdkError as exc:
        json.dump(
            {"ok": False, "error": str(exc), "kind": "sdk"},
            sys.stdout,
            ensure_ascii=False,
        )
        return 4
    except Exception as exc:  # noqa: BLE001 — наружу должен уйти JSON, не трейс
        json.dump(
            {"ok": False, "error": f"{type(exc).__name__}: {exc}", "kind": "other"},
            sys.stdout,
            ensure_ascii=False,
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

    json.dump(
        {
            "ok": True,
            "path": str(Path(raw)),
            "sdk": {"library": status.library, "api_version": status.version},
            "model": payload,
        },
        sys.stdout,
        ensure_ascii=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
