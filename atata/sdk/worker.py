"""Отдельный процесс разбора геометрии через SketchUp SDK.

Запускается как подпроцесс и отдаёт результат JSON-ом::

    python -m atata.sdk.worker model.skp result.json   # в файл (основной режим)
    python -m atata.sdk.worker model.skp               # в stdout, для отладки

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
