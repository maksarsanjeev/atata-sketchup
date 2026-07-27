"""Проверка имён файлов на то, что ломается при переносе на другую машину.

Главное различие, которое здесь проводится: **нелатинское имя само по себе
не сломано.** Современные SketchUp и V-Ray спокойно работают с кириллицей и
иероглифами. Валить их в один список с настоящими поломками — значит
завалить отчёт ложными тревогами.

Поэтому находки делятся на два разряда: ``broken`` — не работает или
работает через раз, и ``risky`` — само по себе законно, но подводит в связке
со старыми экспортёрами и плагинами.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# Windows не разрешает эти символы в имени файла.
ILLEGAL = set('<>:"/\\|?*')

RESERVED = (
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)

# Нулевая ширина, метки направления письма, BOM посреди строки.
INVISIBLE = re.compile(r"[​-‏‪-‮⁠-⁯﻿]")

MAX_NAME = 120


@dataclass(frozen=True)
class NameIssue:
    kind: str  # broken | risky
    code: str
    message: str
    suggestion: str | None = None


def script_of(text: str) -> str:
    """Какими алфавитами написано имя."""
    kinds = set()
    for ch in text:
        if not ch.isalpha():
            continue
        name = unicodedata.name(ch, "")
        if "CJK" in name or "HIRAGANA" in name or "KATAKANA" in name:
            kinds.add("иероглифы")
        elif "HANGUL" in name:
            kinds.add("хангыль")
        elif "CYRILLIC" in name:
            kinds.add("кириллица")
        elif "LATIN" in name:
            kinds.add("латиница")
        elif "ARABIC" in name or "HEBREW" in name:
            kinds.add("арабица/иврит")
        else:
            kinds.add("прочее")
    return "+".join(sorted(kinds)) if kinds else "без букв"


def repair_mojibake(text: str) -> str | None:
    """Вернуть исходное имя, если оно пережило неверную перекодировку.

    Классика: UTF-8 прочитали как cp1251 или cp1252 и сохранили обратно.
    Преобразование обратимо, но применяем его только если результат
    осмысленнее исходника — сменился алфавит и появились буквы.
    """
    if not text:
        return None
    for encoding in ("cp1251", "cp1252", "latin-1"):
        try:
            repaired = text.encode(encoding).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        if repaired == text or not any(ch.isalpha() for ch in repaired):
            continue
        if script_of(repaired) != script_of(text):
            return repaired
    return None


def check(name: str) -> list[NameIssue]:
    """Проверить имя файла. Пустой список — всё в порядке."""
    issues: list[NameIssue] = []
    if not name:
        return issues

    stem, _, _ = name.rpartition(".")
    stem = stem or name

    repaired = repair_mojibake(name)
    if repaired:
        issues.append(
            NameIssue(
                "broken",
                "mojibake",
                "имя пережило неверную перекодировку",
                f"похоже, должно быть «{repaired}»",
            )
        )

    invisible = INVISIBLE.findall(name)
    if invisible:
        codes = " ".join(f"U+{ord(c):04X}" for c in invisible)
        issues.append(
            NameIssue(
                "broken",
                "invisible",
                f"невидимые символы в имени ({codes})",
                "их не видно, но путь из-за них не совпадает",
            )
        )

    if any(unicodedata.category(ch) == "Cc" for ch in name):
        issues.append(NameIssue("broken", "control", "управляющие символы в имени"))

    illegal = ILLEGAL & set(name)
    if illegal:
        issues.append(
            NameIssue(
                "broken",
                "illegal",
                f"недопустимые для Windows символы: {' '.join(sorted(illegal))}",
            )
        )

    # Пробел или точка на конце: Windows молча их срезает, и путь,
    # записанный в проекте, перестаёт совпадать с файлом на диске.
    if name != name.strip() or stem.endswith((" ", ".")):
        issues.append(
            NameIssue(
                "broken",
                "trailing",
                "пробел или точка в конце имени",
                "Windows их срезает, и путь перестаёт совпадать с файлом",
            )
        )

    if stem.upper() in RESERVED:
        issues.append(
            NameIssue(
                "broken",
                "reserved",
                f"«{stem}» — зарезервированное имя устройства в Windows",
            )
        )

    if len(name) > MAX_NAME:
        issues.append(
            NameIssue(
                "risky", "long", f"очень длинное имя ({len(name)} символов)"
            )
        )

    script = script_of(name)
    if script not in ("латиница", "без букв") and not repaired:
        issues.append(
            NameIssue(
                "risky",
                "non_latin",
                f"имя не на латинице ({script})",
                "само по себе законно, но старые экспортёры и плагины на таком спотыкаются",
            )
        )

    return issues
