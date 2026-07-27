"""Консольный прогон анализатора — удобно гонять на реальных файлах без веба.

    python -m atata.cli path/to/model.skp
    python -m atata.cli path/to/model.skp --fix downscale_textures -o out.skp
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .fixes import AVAILABLE_FIXES, apply_fixes
from .rules import analyze
from .skp.container import NotASkpFile
from .skp.facts import collect_facts

BAR = "─" * 72


def human(n: int) -> str:
    for unit, limit in (("ГБ", 1024**3), ("МБ", 1024**2), ("КБ", 1024)):
        if n >= limit:
            return f"{n / limit:.2f} {unit}"
    return f"{n} Б"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="atata", description="порка кривых .skp")
    parser.add_argument("path", type=Path)
    parser.add_argument("--fix", action="append", default=[], choices=sorted(AVAILABLE_FIXES))
    parser.add_argument("-o", "--output", type=Path)
    parser.add_argument("--items", type=int, default=5, help="сколько примеров печатать")
    args = parser.parse_args(argv)

    if not args.path.exists():
        print(f"нет такого файла: {args.path}", file=sys.stderr)
        return 2

    def progress(stage: str, frac: float) -> None:
        print(f"\r  {stage:<28} {frac * 100:5.1f}%", end="", flush=True)

    print(f"{BAR}\nРазбираю {args.path.name} ({human(args.path.stat().st_size)})")
    try:
        facts = collect_facts(args.path, progress=progress)
    except NotASkpFile as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 1
    print()

    print(
        f"  версия SketchUp : {facts.version}\n"
        f"  единицы         : {facts.units}\n"
        f"  записей в архиве: {facts.entry_count}\n"
        f"  геометрия       : {human(facts.model_dat_size)} (сжато {human(facts.model_dat_compressed)})\n"
        f"  материалов      : {len(facts.materials)}\n"
        f"  текстур         : {len(facts.textures)} на {human(facts.texture_bytes)}"
    )

    findings = analyze(facts)
    print(f"{BAR}\nНАЙДЕНО ПРОБЛЕМ: {len(findings)}\n{BAR}")
    for f in findings:
        kind = {"auto": "автомат", "sdk": "нужен SDK", "manual": "руками"}[f.fix_kind]
        impact = f" · ≈{human(f.bytes_impact)}" if f.bytes_impact else ""
        print(f"\n[{f.severity.upper():^8}] {f.title}")
        print(f"           {f.category} · {kind}{impact}")
        for line in f.summary.splitlines():
            if line.strip():
                print(f"           {line.strip()}")
        for item in f.items[: args.items]:
            print(f"             • {item}")
        if len(f.items) > args.items:
            print(f"             … ещё {len(f.items) - args.items}")

    if not args.fix:
        return 0

    dest = args.output or args.path.with_name(args.path.stem + "__atata.skp")
    print(f"\n{BAR}\nПрименяю: {', '.join(args.fix)}\n{BAR}")
    report = apply_fixes(args.path, dest, args.fix, facts, progress=progress)
    print()
    print(
        f"  было   : {human(report.size_before)}\n"
        f"  стало  : {human(report.size_after)}\n"
        f"  снято  : {human(report.saved)}\n"
        f"  тронуто: {len(report.touched)} текстур\n"
        f"  проверка контейнера: {'OK' if report.verified else 'ПРОВАЛ'} — {report.verify_message}"
    )
    for err in report.errors[:10]:
        print(f"  ! {err}")
    print(f"  файл: {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
