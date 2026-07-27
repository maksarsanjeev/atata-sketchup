"""Запуск воркера разбора геометрии подпроцессом.

Три режима, выбираются автоматически:

* **нативный** — Windows или macOS, SDK грузится тем же интерпретатором;
* **wine** — Linux, где SDK крутится под Wine на Windows-питоне
  (проверено: результат совпадает с нативным прогоном до единицы,
  скорость примерно в 1.8 раза ниже);
* **отключён** — SDK не настроен, геометрия не разбирается.

Разбор всегда идёт подпроцессом: модель разворачивается в памяти в разы
больше файла (замер — 4.65 ГБ на файле 348 МБ), и по завершении процесса
память сразу возвращается операционной системе.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .inspect import DefinitionInfo, ModelFacts

DEFAULT_TIMEOUT = int(os.environ.get("ATATA_SDK_TIMEOUT", "900"))


@dataclass
class RunnerConfig:
    mode: str  # native | wine | disabled
    command: list[str]
    reason: str | None = None

    @property
    def enabled(self) -> bool:
        return self.mode != "disabled"


def detect() -> RunnerConfig:
    """Определить, чем запускать воркер."""
    sdk_path = os.environ.get("ATATA_SDK_PATH")

    if platform.system() in ("Windows", "Darwin"):
        return RunnerConfig(mode="native", command=[sys.executable])

    wine = os.environ.get("ATATA_WINE") or shutil.which("wine64") or shutil.which("wine")
    if not wine and Path("/usr/lib/wine/wine64").exists():
        wine = "/usr/lib/wine/wine64"

    win_python = os.environ.get("ATATA_WINE_PYTHON")

    if not wine:
        return RunnerConfig(
            mode="disabled",
            command=[],
            reason=(
                "SDK существует только под Windows и macOS. На Linux его можно "
                "запустить через Wine — установите wine и укажите ATATA_WINE_PYTHON."
            ),
        )
    if not win_python or not Path(win_python).exists():
        return RunnerConfig(
            mode="disabled",
            command=[],
            reason=(
                "Wine найден, но не указан Windows-питон: задайте ATATA_WINE_PYTHON "
                "(путь к python.exe из embeddable-сборки)."
            ),
        )
    if not sdk_path:
        return RunnerConfig(
            mode="disabled",
            command=[],
            reason="не задан ATATA_SDK_PATH — путь к распакованному SketchUp SDK.",
        )

    return RunnerConfig(mode="wine", command=[wine, win_python])


def _run_worker(
    args: list[str],
    config: RunnerConfig,
    timeout: int,
) -> tuple[dict | None, str | None]:
    """Запустить воркер и вернуть (payload, причина отказа)."""
    with tempfile.TemporaryDirectory(prefix="atata-sdk-") as tmp:
        out = Path(tmp) / "result.json"
        cmd = config.command + ["-m", "atata.sdk.worker", *args, str(out)]

        env = dict(os.environ)
        env.setdefault("PYTHONIOENCODING", "utf-8")
        if config.mode == "wine":
            env.setdefault("WINEDEBUG", "-all")
            env.setdefault(
                "WINEPREFIX", os.environ.get("ATATA_WINE_PREFIX", "/tmp/.wine-atata")
            )

        try:
            proc = subprocess.run(
                cmd,
                env=env,
                capture_output=True,
                timeout=timeout,
                # Питон под Wine ищет пакеты рядом с собой; нативному
                # запуску хватает текущего sys.path.
                cwd=str(Path(config.command[-1]).parent)
                if config.mode == "wine"
                else None,
            )
        except subprocess.TimeoutExpired:
            return None, f"воркер не уложился в {timeout} с"
        except OSError as exc:
            return None, f"не удалось запустить воркер: {exc}"

        if not out.exists():
            tail = proc.stderr.decode("utf-8", "replace")[-400:].strip()
            return None, f"воркер не отдал результат (код {proc.returncode}). {tail}"

        try:
            payload = json.loads(out.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return None, f"результат воркера не разобрался: {exc}"

    if not payload.get("ok"):
        return None, payload.get("error", "воркер вернул ошибку без описания")
    return payload, None


def analyze_geometry(
    skp_path: str | Path,
    timeout: int = DEFAULT_TIMEOUT,
    progress: Callable[[str, float], None] | None = None,
) -> tuple[ModelFacts | None, str | None]:
    """Разобрать геометрию. Возвращает (факты, причина отказа)."""
    config = detect()
    if not config.enabled:
        return None, config.reason

    if progress:
        progress(f"разбираю геометрию через SDK ({config.mode})", 0.0)

    payload, error = _run_worker(["inspect", str(skp_path)], config, timeout)
    if error:
        return None, error

    if progress:
        progress("геометрия разобрана", 1.0)
    return from_payload(payload["model"], str(skp_path)), None


def can_open(
    skp_path: str | Path,
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[bool | None, str | None]:
    """Проверить, что файл открывается настоящим читателем SketchUp.

    Возвращает (открывается, причина). ``None`` первым элементом означает,
    что проверить нечем — SDK не настроен.
    """
    config = detect()
    if not config.enabled:
        return None, config.reason

    payload, error = _run_worker(["check", str(skp_path)], config, timeout)
    if error:
        return False, error
    return bool(payload.get("openable")), None


def purge_geometry(
    src: str | Path,
    dest: str | Path,
    timeout: int = DEFAULT_TIMEOUT,
    progress: Callable[[str, float], None] | None = None,
) -> tuple[dict | None, str | None]:
    """Вычистить неиспользуемое и пересохранить модель средствами SDK."""
    config = detect()
    if not config.enabled:
        return None, config.reason

    if progress:
        progress("чищу модель через SDK", 0.0)

    payload, error = _run_worker(
        ["purge", str(src), str(dest)], config, timeout
    )
    if error:
        return None, error
    return payload.get("purge"), None


def from_payload(data: dict, path: str) -> ModelFacts:
    """Собрать ModelFacts обратно из JSON воркера."""
    facts = ModelFacts(
        path=path,
        direct_faces=data.get("direct_faces", 0),
        direct_edges=data.get("direct_edges", 0),
        total_faces=data.get("total_faces", 0),
        total_edges=data.get("total_edges", 0),
        loose_edges=data.get("loose_edges", 0),
        materials=data.get("materials_list", []),
        layers=data.get("layers_list", []),
        scenes=data.get("scenes", 0),
        styles=data.get("styles", 0),
        max_depth=data.get("max_depth", 0),
        truncated=data.get("truncated", False),
    )
    facts.definitions = [
        DefinitionInfo(
            name=d["name"],
            own_faces=d.get("own_faces", 0),
            own_edges=0,
            expanded_faces=d.get("expanded_faces", 0),
            instances=d.get("instances", 0),
            used_instances=d.get("used_instances", 0),
        )
        for d in data.get("definitions_detail", [])
    ]
    return facts
