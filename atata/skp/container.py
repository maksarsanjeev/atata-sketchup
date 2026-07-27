"""Чтение и пересборка контейнера .skp (SketchUp 2021+).

Начиная с SketchUp 2021 файл .skp — это обычный ZIP-архив, перед которым
стоит небольшой бинарный заголовок с тегом формата и строкой версии:

    ff fe ff 0e "SketchUp Model"   (UTF-16LE, длина в символах)
    ff fe ff 0a "{24.0.484}"
    "VFF" + 10 байт служебных данных
    PK\\x03\\x04 ...                 <- дальше обычный ZIP

Смещения в центральном каталоге ZIP записаны **абсолютно**, то есть с учётом
этого префикса. Проверено на реальном файле SketchUp 2024: первый локальный
заголовок лежит на байте 69, и в каталоге записано ровно 69. Благодаря этому
архив можно пересобрать, просто записав префикс обратно и отдав тот же
файловый объект в zipfile — offsets совпадут с исходным соглашением.
"""

from __future__ import annotations

import re
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

# Ищем сигнатуру локального заголовка ZIP только в начале файла — префикс
# заведомо короткий, сканировать весь файл незачем.
_PREFIX_SEARCH_WINDOW = 4096
_ZIP_LOCAL_SIG = b"PK\x03\x04"
_UTF16_STR = re.compile(rb"\xff\xfe\xff(.)", re.DOTALL)
_COPY_CHUNK = 1024 * 1024


class NotASkpFile(ValueError):
    """Файл не похож на .skp нового формата."""


@dataclass(frozen=True)
class Entry:
    """Запись внутри контейнера."""

    name: str
    size: int  # несжатый размер
    compressed_size: int
    compress_type: int

    @property
    def top(self) -> str:
        return self.name.split("/")[0] if "/" in self.name else "(root)"


class SkpContainer:
    """Обёртка над .skp: префикс + ZIP.

    Используется как контекстный менеджер::

        with SkpContainer(path) as skp:
            for entry in skp.entries:
                ...
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.prefix: bytes = b""
        self.header_strings: list[str] = []
        self._zip: zipfile.ZipFile | None = None
        self._open()

    # -- открытие -----------------------------------------------------------

    def _open(self) -> None:
        with open(self.path, "rb") as fh:
            head = fh.read(_PREFIX_SEARCH_WINDOW)

        offset = head.find(_ZIP_LOCAL_SIG)
        if offset == -1:
            raise NotASkpFile(
                "не найден ZIP-контейнер. Похоже, это .skp старого формата "
                "(до SketchUp 2021) — такие читаются только через SketchUp SDK."
            )

        self.prefix = head[:offset]
        self.header_strings = _parse_header_strings(self.prefix)

        try:
            self._zip = zipfile.ZipFile(self.path)
        except zipfile.BadZipFile as exc:
            raise NotASkpFile(f"ZIP-контейнер повреждён: {exc}") from exc

    def close(self) -> None:
        if self._zip is not None:
            self._zip.close()
            self._zip = None

    def __enter__(self) -> "SkpContainer":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # -- чтение -------------------------------------------------------------

    @property
    def zip(self) -> zipfile.ZipFile:
        if self._zip is None:
            raise RuntimeError("контейнер уже закрыт")
        return self._zip

    @property
    def entries(self) -> list[Entry]:
        return [
            Entry(i.filename, i.file_size, i.compress_size, i.compress_type)
            for i in self.zip.infolist()
        ]

    def read(self, name: str) -> bytes:
        return self.zip.read(name)

    def open(self, name: str):
        return self.zip.open(name)

    @property
    def version(self) -> str | None:
        """Строка версии из заголовка, например ``24.0.484``."""
        for s in self.header_strings:
            if s.startswith("{") and s.endswith("}"):
                return s[1:-1]
        return None

    @property
    def file_size(self) -> int:
        return self.path.stat().st_size

    # -- пересборка ---------------------------------------------------------

    def rebuild(
        self,
        dest: str | Path,
        selector: Callable[[Entry], bool] | None = None,
        transform: Callable[[Entry, bytes], bytes | None] | None = None,
        drop: Callable[[Entry], bool] | None = None,
        progress: Callable[[int, int], None] | None = None,
    ) -> "RebuildResult":
        """Пересобрать контейнер в новый файл.

        ``selector`` заранее отвечает, интересует ли нас содержимое записи.
        Только выбранные записи читаются в память целиком и отдаются в
        ``transform``; всё остальное копируется потоком.

        Разделение здесь не косметическое: ``model.dat`` в реальном проекте
        занимает под гигабайт в несжатом виде, и чтение его в память
        гарантированно роняет контейнер с лимитом в полтора гигабайта.

        ``transform`` возвращает новое содержимое либо ``None``, если менять
        нечего. ``drop`` позволяет выкинуть запись целиком.
        """
        dest = Path(dest)
        infos = self.zip.infolist()
        total = len(infos)

        changed: list[str] = []
        dropped: list[str] = []
        bytes_before = 0
        bytes_after = 0

        with open(dest, "wb") as out:
            # Префикс должен лечь первым, чтобы zipfile посчитал абсолютные
            # смещения ровно так же, как это делает сам SketchUp.
            out.write(self.prefix)

            with zipfile.ZipFile(out, "w") as zout:
                for index, info in enumerate(infos):
                    entry = Entry(
                        info.filename,
                        info.file_size,
                        info.compress_size,
                        info.compress_type,
                    )
                    bytes_before += info.file_size

                    if drop is not None and drop(entry):
                        dropped.append(entry.name)
                        if progress:
                            progress(index + 1, total)
                        continue

                    # Сохраняем исходный способ сжатия: у картинок он STORED,
                    # пережимать их deflate-ом бессмысленно и только медленнее.
                    new_info = _clone_info(info)

                    if info.is_dir():
                        zout.writestr(new_info, b"")
                    elif transform is not None and (selector is None or selector(entry)):
                        data = self.zip.read(info.filename)
                        replacement = transform(entry, data)
                        if replacement is not None and replacement != data:
                            data = replacement
                            changed.append(entry.name)
                        zout.writestr(new_info, data)
                        bytes_after += len(data)
                    else:
                        with self.zip.open(info) as src, zout.open(new_info, "w") as dst:
                            shutil.copyfileobj(src, dst, _COPY_CHUNK)
                        bytes_after += info.file_size

                    if progress:
                        progress(index + 1, total)

        return RebuildResult(
            dest=dest,
            changed=changed,
            dropped=dropped,
            bytes_before=bytes_before,
            bytes_after=bytes_after,
            size_before=self.file_size,
            size_after=dest.stat().st_size,
        )

    def copy_to(self, dest: str | Path) -> Path:
        dest = Path(dest)
        shutil.copy2(self.path, dest)
        return dest


@dataclass
class RebuildResult:
    dest: Path
    changed: list[str]
    dropped: list[str]
    bytes_before: int
    bytes_after: int
    size_before: int
    size_after: int

    @property
    def saved(self) -> int:
        return self.size_before - self.size_after


def _clone_info(info: zipfile.ZipInfo) -> zipfile.ZipInfo:
    """Скопировать метаданные записи, не перенося смещения и контрольные суммы."""
    clone = zipfile.ZipInfo(info.filename, date_time=info.date_time)
    clone.compress_type = info.compress_type
    clone.external_attr = info.external_attr
    clone.internal_attr = info.internal_attr
    clone.create_system = info.create_system
    clone.comment = info.comment
    return clone


def _parse_header_strings(prefix: bytes) -> list[str]:
    """Вытащить UTF-16LE строки с префиксом длины из заголовка."""
    out: list[str] = []
    for match in _UTF16_STR.finditer(prefix):
        length = match.group(1)[0]
        start = match.end()
        raw = prefix[start : start + length * 2]
        if len(raw) == length * 2:
            try:
                out.append(raw.decode("utf-16-le"))
            except UnicodeDecodeError:
                continue
    return out


def verify(path: str | Path) -> tuple[bool, str]:
    """Проверить, что файл открывается как .skp и все записи читаются.

    Это проверка целостности контейнера, а не гарантия, что SketchUp
    примет файл: содержимое ``model.dat`` здесь не валидируется.
    """
    try:
        with SkpContainer(path) as skp:
            bad = skp.zip.testzip()
            if bad is not None:
                return False, f"битая запись в архиве: {bad}"
            names = {e.name for e in skp.entries}
            if "model.dat" not in names:
                return False, "в контейнере нет model.dat"
            if skp.version is None:
                return False, "в заголовке нет строки версии"
            return True, f"контейнер целый, {len(names)} записей, версия {skp.version}"
    except NotASkpFile as exc:
        return False, str(exc)


def iter_names(entries: Iterable[Entry], prefix: str) -> list[Entry]:
    return [e for e in entries if e.name.startswith(prefix)]
