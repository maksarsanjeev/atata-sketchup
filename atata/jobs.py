"""Простая очередь задач в процессе.

Redis/Celery здесь были бы оверинжинирингом: анализ .skp — это одна тяжёлая
CPU-задача на файл, параллелить её на одноядерной машине смысла нет.
Когда появится SDK-этап и несколько воркеров, эта прослойка меняется на
нормальный брокер, интерфейс остаётся тем же.
"""

from __future__ import annotations

import os
import shutil
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Сколько тяжёлых задач крутится одновременно. Считать надо от памяти, а не
# от ядер: требования по RAM умножаются на число параллельных разборов.
# На этапе SketchUp SDK это должно быть 1 — модель разворачивается в памяти
# в разы больше, чем весит файл.
MAX_WORKERS = int(os.environ.get("ATATA_MAX_WORKERS", "2"))
JOB_TTL_SECONDS = int(os.environ.get("ATATA_JOB_TTL_HOURS", "6")) * 3600


@dataclass
class Job:
    id: str
    kind: str  # analyze | fix
    filename: str
    dir: Path
    status: str = "queued"  # queued | running | done | error
    stage: str = "в очереди"
    progress: float = 0.0
    created: float = field(default_factory=time.time)
    finished: float | None = None
    result: dict[str, Any] | None = None
    error: str | None = None

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "filename": self.filename,
            "status": self.status,
            "stage": self.stage,
            "progress": round(self.progress, 3),
            "created": self.created,
            "elapsed": round((self.finished or time.time()) - self.created, 1),
            "error": self.error,
        }


class JobStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._jobs: dict[str, Job] = {}
        self._facts: dict[str, Any] = {}
        self._lock = threading.Lock()
        self._slots = threading.Semaphore(MAX_WORKERS)

    # -- жизненный цикл -----------------------------------------------------

    def create(self, kind: str, filename: str) -> Job:
        job_id = uuid.uuid4().hex[:12]
        job_dir = self.root / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        job = Job(id=job_id, kind=kind, filename=filename, dir=job_dir)
        with self._lock:
            self._jobs[job_id] = job
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def run(self, job: Job, target, *args, **kwargs) -> None:
        """Запустить задачу в фоне."""

        def runner() -> None:
            self._slots.acquire()
            job.status = "running"
            job.stage = "стартую"
            try:
                job.result = target(job, *args, **kwargs)
                job.status = "done"
                job.stage = "готово"
                job.progress = 1.0
            except Exception as exc:
                job.status = "error"
                job.stage = "упало"
                job.error = f"{type(exc).__name__}: {exc}"
                traceback.print_exc()
            finally:
                job.finished = time.time()
                self._slots.release()

        threading.Thread(target=runner, name=f"job-{job.id}", daemon=True).start()

    # -- побочные данные ----------------------------------------------------

    def stash_facts(self, job_id: str, facts: Any) -> None:
        with self._lock:
            self._facts[job_id] = facts

    def facts(self, job_id: str) -> Any | None:
        with self._lock:
            return self._facts.get(job_id)

    # -- уборка -------------------------------------------------------------

    def cleanup(self) -> int:
        """Снести задачи старше TTL. Диск на дев-стенде маленький, файлы большие."""
        now = time.time()
        removed = 0
        with self._lock:
            stale = [j for j in self._jobs.values() if now - j.created > JOB_TTL_SECONDS]
            for job in stale:
                self._jobs.pop(job.id, None)
                self._facts.pop(job.id, None)
                shutil.rmtree(job.dir, ignore_errors=True)
                removed += 1

        # Осиротевшие каталоги от прошлых запусков процесса.
        known = set(self._jobs)
        for path in self.root.iterdir():
            if path.is_dir() and path.name not in known:
                if now - path.stat().st_mtime > JOB_TTL_SECONDS:
                    shutil.rmtree(path, ignore_errors=True)
                    removed += 1
        return removed

    def disk_usage(self) -> int:
        return sum(p.stat().st_size for p in self.root.rglob("*") if p.is_file())
