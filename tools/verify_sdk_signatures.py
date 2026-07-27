"""Сверка ctypes-объявлений с заголовками SketchUp C API.

Обвязка в :mod:`atata.sdk.capi` объявляет функции вручную, а значит может
разъехаться с реальным API — молча и с непредсказуемыми последствиями.
Этот скрипт читает заголовки вашей копии SDK и проверяет каждое объявление.

Запуск:

    ATATA_SDK_PATH=/path/to/SDK python tools/verify_sdk_signatures.py

Проверяется имя, число аргументов и тип возврата (часть функций C API
объявлена как ``void``, а не ``SU_RESULT`` — если перепутать, обвязка
начнёт проверять мусор в регистре как код ошибки).
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from atata.sdk.capi import _SIGNATURES, VOID_FUNCTIONS  # noqa: E402

DECL = re.compile(
    r"(?P<ret>SU_RESULT|void)\s+(?P<name>SU\w+)\s*\((?P<params>[^)]*)\)",
    re.DOTALL,
)


def strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    text = re.sub(r"//[^\n]*", "", text)
    return text


def collect_declarations(headers: Path) -> dict[str, tuple[str, int]]:
    found: dict[str, tuple[str, int]] = {}
    for path in headers.rglob("*.h"):
        code = strip_comments(path.read_text(encoding="utf-8", errors="replace"))
        code = re.sub(r"\s+", " ", code)
        for match in DECL.finditer(code):
            params = match.group("params").strip()
            count = 0 if params in ("", "void") else params.count(",") + 1
            found[match.group("name")] = (match.group("ret"), count)
    return found


def main() -> int:
    root = os.environ.get("ATATA_SDK_PATH")
    if not root:
        print("нужен ATATA_SDK_PATH — путь к распакованному SDK", file=sys.stderr)
        return 2

    headers = Path(root) / "headers"
    if not headers.is_dir():
        headers = Path(root)
    if not headers.is_dir():
        print(f"не нашёл заголовки в {root}", file=sys.stderr)
        return 2

    declared = collect_declarations(headers)
    print(f"прочитано объявлений: {len(declared)} из {headers}\n")

    problems = 0
    for name, argtypes in _SIGNATURES:
        actual = declared.get(name)
        if actual is None:
            print(f"!! {name:44s} нет в заголовках")
            problems += 1
            continue

        ret, count = actual
        expect_void = name in VOID_FUNCTIONS
        issues = []
        if count != len(argtypes):
            issues.append(f"аргументов {len(argtypes)} против {count}")
        if (ret == "void") != expect_void:
            issues.append(
                f"тип возврата {ret}, а в обвязке "
                f"{'void' if expect_void else 'SUResult'}"
            )

        if issues:
            print(f"!! {name:44s} " + "; ".join(issues))
            problems += 1
        else:
            print(f"   {name:44s} ok")

    print(f"\nпроверено {len(_SIGNATURES)}, расхождений: {problems}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
