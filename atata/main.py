"""HTTP-морда сервиса."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from . import __version__
from .fixes import AVAILABLE_FIXES, apply_fixes
from .jobs import Job, JobStore
from .rules import analyze
from .sdk import SdkError, SdkUnavailable, inspect_model, sdk_status
from .skp.container import NotASkpFile
from .skp.facts import collect_facts

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("ATATA_DATA_DIR", BASE_DIR.parent / "data"))
MAX_UPLOAD_MB = int(os.environ.get("ATATA_MAX_UPLOAD_MB", "1024"))
CHUNK = 4 * 1024 * 1024

app = FastAPI(title="atata-sketchup", version=__version__)
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

store = JobStore(DATA_DIR / "jobs")


# --------------------------------------------------------------------------
# Страницы
# --------------------------------------------------------------------------


@app.get("/")
async def index(request: Request):
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "version": __version__, "max_mb": MAX_UPLOAD_MB},
    )


@app.get("/health")
async def health():
    return {
        "ok": True,
        "version": __version__,
        "disk_used_mb": round(store.disk_usage() / 1024 / 1024, 1),
        "sdk": sdk_status().as_dict(),
    }


# --------------------------------------------------------------------------
# Загрузка и анализ
# --------------------------------------------------------------------------


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    if not file.filename or not file.filename.lower().endswith(".skp"):
        raise HTTPException(400, "нужен файл с расширением .skp")

    store.cleanup()
    job = store.create("analyze", file.filename)
    dest = job.dir / "original.skp"

    size = 0
    limit = MAX_UPLOAD_MB * 1024 * 1024
    try:
        with open(dest, "wb") as out:
            while chunk := await file.read(CHUNK):
                size += len(chunk)
                if size > limit:
                    raise HTTPException(413, f"файл больше лимита в {MAX_UPLOAD_MB} МБ")
                out.write(chunk)
    except HTTPException:
        shutil.rmtree(job.dir, ignore_errors=True)
        raise
    finally:
        await file.close()

    if size == 0:
        shutil.rmtree(job.dir, ignore_errors=True)
        raise HTTPException(400, "пустой файл")

    store.run(job, _analyze_task, dest)
    return {"job_id": job.id, "filename": file.filename, "size": size}


def _analyze_task(job: Job, path: Path) -> dict:
    def progress(stage: str, frac: float) -> None:
        job.stage = stage
        job.progress = frac

    try:
        facts = collect_facts(path, progress=progress)
    except NotASkpFile as exc:
        raise HTTPException(415, str(exc)) from exc

    # Разбор геометрии — только там, где есть SketchUp SDK (Windows/macOS).
    # На Linux слой отсутствует, и это не ошибка: отчёт просто остаётся
    # на косвенных оценках контейнерного слоя.
    sdk_note: str | None = None
    try:
        job.stage = "разбираю геометрию через SDK"
        facts.model = inspect_model(path, progress=progress)
    except SdkUnavailable as exc:
        sdk_note = str(exc)
    except SdkError as exc:
        sdk_note = f"SDK не смог прочитать модель: {exc}"

    store.stash_facts(job.id, facts)

    job.stage = "применяю правила"
    findings = analyze(facts)

    auto_saveable = sum(f.bytes_impact for f in findings if f.fix_kind == "auto")
    by_severity: dict[str, int] = {}
    for f in findings:
        by_severity[f.severity] = by_severity.get(f.severity, 0) + 1

    return {
        "file": {
            "name": facts.filename,
            "size": facts.file_size,
            "version": facts.version,
            "units": facts.units,
            "entries": facts.entry_count,
            "model_dat_size": facts.model_dat_size,
            "materials": len(facts.materials),
            "textures": len(facts.textures),
            "texture_bytes": facts.texture_bytes,
        },
        "composition": [
            {
                "group": group,
                "bytes": size,
                "count": facts.group_counts.get(group, 0),
            }
            for group, size in sorted(
                facts.group_sizes.items(), key=lambda kv: -kv[1]
            )
        ],
        "findings": [f.as_dict() for f in findings],
        "summary": {
            "total": len(findings),
            "by_severity": by_severity,
            "auto_saveable_bytes": auto_saveable,
        },
        "sdk": {
            "used": facts.model is not None,
            "note": sdk_note,
            "model": facts.model.as_dict() if facts.model is not None else None,
        },
        "fixes": {k: v.__dict__ for k, v in AVAILABLE_FIXES.items()},
    }


# --------------------------------------------------------------------------
# Статус и отчёт
# --------------------------------------------------------------------------


@app.get("/api/job/{job_id}")
async def job_status(job_id: str):
    job = store.get(job_id)
    if job is None:
        raise HTTPException(404, "задача не найдена — возможно, её уже вычистили по TTL")
    payload = job.as_dict()
    if job.status == "done":
        payload["result"] = job.result
    return JSONResponse(payload)


# --------------------------------------------------------------------------
# Наказание
# --------------------------------------------------------------------------


@app.post("/api/job/{job_id}/fix")
async def start_fix(job_id: str, payload: dict):
    source = store.get(job_id)
    if source is None:
        raise HTTPException(404, "задача не найдена")
    if source.status != "done":
        raise HTTPException(409, "анализ ещё не закончен")

    facts = store.facts(job_id)
    if facts is None:
        raise HTTPException(410, "данные анализа уже вычищены, загрузите файл заново")

    requested = payload.get("fixes") or []
    applicable = [f for f in requested if f in AVAILABLE_FIXES]
    if not applicable:
        raise HTTPException(
            400,
            "ни одно из выбранных исправлений пока не применяется автоматически — "
            "остальные ждут этапа SketchUp SDK",
        )

    job = store.create("fix", source.filename)
    src = source.dir / "original.skp"
    dest = job.dir / _fixed_name(source.filename)
    store.run(job, _fix_task, src, dest, applicable, facts)
    return {"job_id": job.id, "fixes": applicable}


def _fix_task(job: Job, src: Path, dest: Path, fix_ids: list[str], facts) -> dict:
    def progress(stage: str, frac: float) -> None:
        job.stage = stage
        job.progress = frac

    report = apply_fixes(src, dest, fix_ids, facts, progress=progress)
    return report.as_dict()


@app.get("/api/job/{job_id}/download")
async def download(job_id: str):
    job = store.get(job_id)
    if job is None or job.status != "done" or job.kind != "fix":
        raise HTTPException(404, "готового файла нет")
    candidates = list(job.dir.glob("*.skp"))
    if not candidates:
        raise HTTPException(404, "файл не найден на диске")
    path = candidates[0]
    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename=path.name,
    )


def _fixed_name(original: str) -> str:
    stem = Path(original).stem
    return f"{stem}__atata.skp"
