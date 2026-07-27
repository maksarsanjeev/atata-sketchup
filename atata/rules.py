"""Правила поиска проблем.

Каждое правило — функция ``SkpFacts -> Finding | None``. Регистрируются
декоратором :func:`rule`, порядок вывода задаётся severity.

Правила разделены по слою, на котором они работают:

* ``container`` — всё, что видно из ZIP-контейнера. Работает уже сейчас.
* ``model``     — требует разбора ``model.dat`` через SketchUp SDK.
                  Такие правила пока умеют только оценивать масштаб беды
                  по косвенным признакам и честно помечают себя ``fix_kind="sdk"``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

from .skp.facts import SkpFacts, TextureInfo, hamming

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}

# Порог, выше которого текстура считается переразмеренной для интерьерной
# визуализации. 2048 — компромисс: детализация ещё есть, вес уже вменяемый.
MAX_TEXTURE_DIM = 2048
BIG_TEXTURE_BYTES = 1024 * 1024
NEAR_DUPLICATE_DISTANCE = 5
MATERIAL_COUNT_WARN = 200
MATERIAL_COUNT_CRIT = 800
MODEL_DAT_WARN = 200 * 1024 * 1024
MODEL_DAT_CRIT = 600 * 1024 * 1024


@dataclass
class Finding:
    id: str
    severity: str
    category: str
    title: str
    summary: str
    layer: str = "container"
    items: list[str] = field(default_factory=list)
    count: int = 0
    bytes_impact: int = 0
    fix: str | None = None
    fix_kind: str = "manual"  # auto | sdk | manual
    fix_label: str | None = None
    fix_note: str | None = None

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "severity": self.severity,
            "category": self.category,
            "title": self.title,
            "summary": self.summary,
            "layer": self.layer,
            "items": self.items[:40],
            "items_total": len(self.items),
            "count": self.count,
            "bytes_impact": self.bytes_impact,
            "fix": self.fix,
            "fix_kind": self.fix_kind,
            "fix_label": self.fix_label,
            "fix_note": self.fix_note,
        }


RuleFn = Callable[[SkpFacts], Finding | None]
RULES: list[RuleFn] = []


def rule(fn: RuleFn) -> RuleFn:
    RULES.append(fn)
    return fn


def analyze(facts: SkpFacts) -> list[Finding]:
    found: list[Finding] = []
    for fn in RULES:
        try:
            result = fn(facts)
        except Exception as exc:  # одно кривое правило не должно ронять отчёт
            found.append(
                Finding(
                    id=f"rule-error:{fn.__name__}",
                    severity="info",
                    category="служебное",
                    title=f"Правило {fn.__name__} упало",
                    summary=f"{type(exc).__name__}: {exc}",
                )
            )
            continue
        if result is not None:
            found.append(result)
    found.sort(key=lambda f: (SEVERITY_ORDER.get(f.severity, 9), -f.bytes_impact))
    return found


# --------------------------------------------------------------------------
# Файл целиком
# --------------------------------------------------------------------------


@rule
def r_file_size(f: SkpFacts) -> Finding | None:
    mb = f.file_size / 1024 / 1024
    if mb < 100:
        return None
    severity = "critical" if mb > 300 else "high" if mb > 200 else "medium"
    return Finding(
        id="file.size",
        severity=severity,
        category="файл",
        title=f"Файл весит {mb:.0f} МБ",
        summary=(
            f"Проект на {mb:.0f} МБ открывается долго, тормозит при навигации и "
            f"тяжело переживает автосохранение. Ниже разбор, из чего этот вес состоит."
        ),
        count=1,
    )


@rule
def r_source_path(f: SkpFacts) -> Finding | None:
    if not f.source_path:
        return None
    looks_internal = f.source_path.startswith("\\\\") or ":" in f.source_path[:3]
    if not looks_internal:
        return None
    return Finding(
        id="privacy.source_path",
        severity="low",
        category="гигиена",
        title="В файле сохранён путь с внутренней сетевой шары",
        summary=(
            "SketchUp пишет в метаданные полный путь последнего сохранения. "
            "Если файл уходит заказчику или подрядчику, вместе с ним уезжает "
            "структура вашей файлопомойки и имена сотрудников."
        ),
        items=[f.source_path],
        count=1,
        fix_kind="sdk",
        fix_note="Очистка метаданных требует пересохранения через SketchUp SDK.",
    )


# --------------------------------------------------------------------------
# Геометрия (косвенно, до подключения SDK)
# --------------------------------------------------------------------------


@rule
def r_model_dat_weight(f: SkpFacts) -> Finding | None:
    # Когда SDK доступен, вес заменяется настоящим разбором — см. rules_model.
    if f.model is not None:
        return None
    if f.model_dat_size < MODEL_DAT_WARN:
        return None
    mb = f.model_dat_size / 1024 / 1024
    share = f.model_dat_compressed / f.file_size * 100 if f.file_size else 0
    severity = "critical" if f.model_dat_size > MODEL_DAT_CRIT else "high"
    return Finding(
        id="model.weight",
        severity=severity,
        category="геометрия",
        title=f"Геометрия занимает {mb:.0f} МБ в несжатом виде",
        summary=(
            f"`model.dat` — это вся геометрия, компоненты, слои и сцены. "
            f"{mb:.0f} МБ несжатых данных (это {share:.0f}% веса файла) для "
            f"интерьерной сцены означает либо импортированные модели с диким "
            f"полигонажем, либо гору неочищенных определений компонентов.\n\n"
            f"Точный разбор — сколько рёбер, какие компоненты самые тяжёлые, что "
            f"реально не используется — возможен только через SketchUp SDK."
        ),
        layer="model",
        count=1,
        fix_kind="sdk",
        fix_label="Разобрать геометрию",
        fix_note="Ждёт подключения SketchUp SDK (этап 2).",
    )


# --------------------------------------------------------------------------
# Материалы
# --------------------------------------------------------------------------


@rule
def r_material_count(f: SkpFacts) -> Finding | None:
    n = len(f.materials)
    if n < MATERIAL_COUNT_WARN:
        return None
    severity = "critical" if n > MATERIAL_COUNT_CRIT else "high"
    flat = sum(1 for m in f.materials if not m.textures)
    return Finding(
        id="materials.count",
        severity=severity,
        category="материалы",
        title=f"{n} материалов в одном файле",
        summary=(
            f"Из них {flat} — просто цвет без текстуры. Столько материалов в "
            f"одном проекте не создают руками: это накопленный мусор от импорта "
            f"чужих компонентов. Каждый лишний материал висит в палитре и мешает "
            f"работать.\n\nСколько из них реально назначено хоть на одну грань — "
            f"видно только через SDK; purge неиспользуемых обычно убирает больше половины."
        ),
        count=n,
        fix_kind="sdk",
        fix_label="Вычистить неиспользуемые",
        fix_note="Purge неиспользуемых материалов требует SketchUp SDK (этап 2).",
    )


@rule
def r_material_families(f: SkpFacts) -> Finding | None:
    families: dict[str, list[str]] = {}
    for m in f.materials:
        base = re.sub(r"(#\d+|\d+)$", "", m.name).strip()
        if not base:
            continue
        families.setdefault(base, []).append(m.name)

    big = {k: v for k, v in families.items() if len(v) > 2}
    if not big:
        return None

    extra = sum(len(v) - 1 for v in big.values())
    items = [
        f"{len(v)}× «{k}»"
        for k, v in sorted(big.items(), key=lambda kv: -len(kv[1]))
    ]
    return Finding(
        id="materials.families",
        severity="high",
        category="материалы",
        title=f"{len(big)} семейств материалов-клонов ({extra} лишних)",
        summary=(
            "Имена вида `Материал`, `Материал2`, `Материал#3` SketchUp создаёт сам, "
            "когда при импорте компонента встречает материал с уже занятым именем. "
            "Обычно это один и тот же материал, размноженный десяток раз.\n\n"
            "Свести их в один можно только через SDK: нужно переназначить материал "
            "на гранях, а грани живут в `model.dat`."
        ),
        items=items,
        count=extra,
        fix_kind="sdk",
        fix_label="Схлопнуть клоны",
        fix_note="Требует переназначения на гранях — SketchUp SDK (этап 2).",
    )


@rule
def r_material_auto_generated(f: SkpFacts) -> Finding | None:
    auto = [m.name for m in f.materials if m.name.lower().startswith("_auto_")]
    if not auto:
        return None
    return Finding(
        id="materials.auto",
        severity="medium",
        category="материалы",
        title=f"{len(auto)} материалов с именем `_auto_*`",
        summary=(
            "Такие имена генерируют импортёры (чаще всего при заходе из 3ds Max / FBX / "
            "DWG). Осмысленного имени у них нет, отличить один от другого в палитре "
            "невозможно."
        ),
        items=sorted(auto),
        count=len(auto),
        fix_kind="sdk",
        fix_note="Переименование/слияние — SketchUp SDK (этап 2).",
    )


@rule
def r_material_default_names(f: SkpFacts) -> Finding | None:
    pattern = re.compile(r"^(Material|Материал|Image|Color|Generic|_)[\s_~#]*\d*$", re.I)
    hits = [m.name for m in f.materials if pattern.match(m.name)]
    if len(hits) < 5:
        return None
    return Finding(
        id="materials.default_names",
        severity="low",
        category="материалы",
        title=f"{len(hits)} материалов с дефолтным именем",
        summary=(
            "`Material12`, `Image`, `Color_3` — имена, которые SketchUp даёт сам. "
            "На такой палитре невозможно собрать спецификацию и невозможно понять, "
            "что где применено."
        ),
        items=sorted(hits),
        count=len(hits),
        fix_kind="manual",
        fix_note="Осмысленные имена может дать только человек.",
    )


# --------------------------------------------------------------------------
# Текстуры
# --------------------------------------------------------------------------


@rule
def r_texture_total(f: SkpFacts) -> Finding | None:
    total = f.texture_bytes
    if total < 20 * 1024 * 1024:
        return None
    mb = total / 1024 / 1024
    return Finding(
        id="textures.total",
        severity="medium" if mb < 100 else "high",
        category="текстуры",
        title=f"Текстуры весят {mb:.0f} МБ",
        summary=(
            f"{len(f.textures)} растровых файлов внутри контейнера. Они лежат уже "
            f"сжатыми (JPEG/PNG), поэтому ZIP их не ужимает — вес переносится в "
            f"итоговый файл один в один."
        ),
        count=len(f.textures),
        bytes_impact=total,
    )


@rule
def r_texture_oversized(f: SkpFacts) -> Finding | None:
    big = [
        t
        for t in f.textures
        if (t.width or 0) > MAX_TEXTURE_DIM
        or (t.height or 0) > MAX_TEXTURE_DIM
        or t.bytes > BIG_TEXTURE_BYTES
    ]
    if not big:
        return None
    big.sort(key=lambda t: -t.bytes)

    saveable = sum(_estimate_downscale_saving(t) for t in big)
    items = [
        f"{t.bytes / 1024 / 1024:.2f} МБ · {t.width}×{t.height} · {t.material}/{t.filename}"
        for t in big
    ]
    return Finding(
        id="textures.oversized",
        severity="high",
        category="текстуры",
        title=f"{len(big)} переразмеренных текстур на {sum(t.bytes for t in big) / 1024 / 1024:.0f} МБ",
        summary=(
            f"Текстура больше {MAX_TEXTURE_DIM}px по стороне в интерьерной сцене почти "
            f"никогда не отрабатывает: на экране она занимает от силы несколько сотен "
            f"пикселей. При этом она целиком грузится в видеопамять и замедляет вьюпорт.\n\n"
            f"Даунскейл до {MAX_TEXTURE_DIM}px по длинной стороне — самая дешёвая "
            f"экономия в этом файле."
        ),
        items=items,
        count=len(big),
        bytes_impact=saveable,
        fix_kind="sdk",
        fix_label=f"Ужать до {MAX_TEXTURE_DIM}px",
        fix_note=(
            "Автозамена временно снята: подменять картинки прямо в контейнере "
            "нельзя — SketchUp перестаёт открывать такой файл. Переделывается "
            "через SDK."
        ),
    )


@rule
def r_texture_exact_duplicates(f: SkpFacts) -> Finding | None:
    by_hash: dict[str, list[TextureInfo]] = {}
    for t in f.textures:
        if t.sha1:
            by_hash.setdefault(t.sha1, []).append(t)
    groups = [v for v in by_hash.values() if len(v) > 1]
    if not groups:
        return None

    wasted = sum(g[0].bytes * (len(g) - 1) for g in groups)
    items = [
        f"{g[0].bytes / 1024:.0f} КБ ×{len(g)} · {g[0].filename} → "
        + ", ".join(t.material for t in g[:5])
        for g in sorted(groups, key=lambda g: -g[0].bytes * (len(g) - 1))
    ]
    return Finding(
        id="textures.exact_duplicates",
        severity="medium",
        category="текстуры",
        title=f"{len(groups)} групп побайтово одинаковых текстур",
        summary=(
            "Один и тот же файл лежит под несколькими материалами. Обычно это "
            "следствие импорта одной и той же модели из разных источников."
        ),
        items=items,
        count=sum(len(g) - 1 for g in groups),
        bytes_impact=wasted,
        fix_kind="sdk",
        fix_note="Слияние материалов требует переназначения на гранях — SDK (этап 2).",
    )


@rule
def r_texture_near_duplicates(f: SkpFacts) -> Finding | None:
    # Только текстуры с осмысленным перцептивным хешем: у однотонных заливок
    # dhash вырождается в 0 или все единицы и даёт ложные совпадения.
    pool = [
        t
        for t in f.textures
        if t.dhash is not None and t.dhash not in (0, (1 << 64) - 1)
    ]
    if len(pool) < 2 or len(pool) > 4000:
        return None

    seen: set[int] = set()
    groups: list[list[TextureInfo]] = []
    for i, a in enumerate(pool):
        if i in seen:
            continue
        group = [a]
        for j in range(i + 1, len(pool)):
            if j in seen:
                continue
            b = pool[j]
            if a.sha1 == b.sha1:
                continue  # это уже поймало правило точных дублей
            if hamming(a.dhash, b.dhash) <= NEAR_DUPLICATE_DISTANCE:
                group.append(b)
                seen.add(j)
        if len(group) > 1:
            seen.add(i)
            groups.append(group)

    if not groups:
        return None

    wasted = sum(sum(t.bytes for t in g[1:]) for g in groups)
    items = [
        " ≈ ".join(f"{t.material}/{t.filename} ({t.width}×{t.height})" for t in g[:4])
        for g in sorted(groups, key=lambda g: -sum(t.bytes for t in g[1:]))
    ]
    return Finding(
        id="textures.near_duplicates",
        severity="medium",
        category="текстуры",
        title=f"{len(groups)} групп визуально одинаковых текстур",
        summary=(
            "Совпадают по перцептивному хешу, но не побайтово — то есть это одна "
            "и та же картинка, пересохранённая с другим качеством или размером.\n\n"
            "Проверьте глазами перед слиянием: на однотонных и мелкоузорчатых "
            "текстурах перцептивный хеш может ошибаться."
        ),
        items=items,
        count=sum(len(g) - 1 for g in groups),
        bytes_impact=wasted,
        fix_kind="sdk",
        fix_note="Слияние требует SDK (этап 2). Список — для ручной проверки.",
    )


@rule
def r_texture_npot(f: SkpFacts) -> Finding | None:
    npot = [
        t
        for t in f.textures
        if t.width and t.height and (t.width & (t.width - 1) or t.height & (t.height - 1))
    ]
    if len(npot) < 5:
        return None
    return Finding(
        id="textures.npot",
        severity="low",
        category="текстуры",
        title=f"{len(npot)} текстур не кратны степени двойки",
        summary=(
            f"{len(npot)} из {len(f.textures)}. Видеокарта под мипмаппинг всё равно "
            f"дополняет такую текстуру до ближайшей степени двойки — то есть память "
            f"тратится как на большую, а детализация остаётся как у меньшей."
        ),
        items=[
            f"{t.width}×{t.height} · {t.material}/{t.filename}"
            for t in sorted(npot, key=lambda t: -t.bytes)
        ],
        count=len(npot),
        fix_kind="sdk",
        fix_note=(
            "Автозамена временно снята вместе с остальными правками текстур — "
            "см. выше."
        ),
    )


@rule
def r_texture_extreme_aspect(f: SkpFacts) -> Finding | None:
    hits = []
    for t in f.textures:
        if not t.width or not t.height:
            continue
        ratio = max(t.width, t.height) / min(t.width, t.height)
        if ratio >= 4 and t.bytes > 256 * 1024:
            hits.append((ratio, t))
    if not hits:
        return None
    hits.sort(key=lambda x: -x[0])
    return Finding(
        id="textures.aspect",
        severity="low",
        category="текстуры",
        title=f"{len(hits)} текстур с экстремальными пропорциями",
        summary=(
            "Соотношение сторон 4:1 и выше. Часто это случайно растянутый скриншот "
            "или неудачно вырезанный кусок развёртки."
        ),
        items=[
            f"{r:.1f}:1 · {t.width}×{t.height} · {t.material}/{t.filename}"
            for r, t in hits
        ],
        count=len(hits),
        fix_kind="manual",
    )


@rule
def r_texture_png_without_alpha(f: SkpFacts) -> Finding | None:
    hits = [
        t
        for t in f.textures
        if t.fmt == "PNG" and not t.has_alpha and t.bytes > 512 * 1024
    ]
    if not hits:
        return None
    hits.sort(key=lambda t: -t.bytes)
    total = sum(t.bytes for t in hits)
    return Finding(
        id="textures.png_no_alpha",
        severity="low",
        category="текстуры",
        title=f"{len(hits)} тяжёлых PNG без прозрачности",
        summary=(
            "PNG сжимает фотографические текстуры заметно хуже JPEG, а смысл в нём "
            "только ради альфа-канала — которого здесь нет. Пересохранение в JPEG "
            "обычно уменьшает такую текстуру в 5–10 раз.\n\n"
            "Автозамена не делается: смена расширения рвёт ссылку из `model.dat`, "
            "переписать её можно только через SDK."
        ),
        items=[f"{t.bytes / 1024 / 1024:.2f} МБ · {t.material}/{t.filename}" for t in hits],
        count=len(hits),
        bytes_impact=int(total * 0.7),
        fix_kind="sdk",
    )


_EXPECTED_EXT = {
    "JPEG": {".jpg", ".jpeg"},
    "PNG": {".png"},
    "TIFF": {".tif", ".tiff"},
    "BMP": {".bmp"},
    "GIF": {".gif"},
    "WEBP": {".webp"},
    "TGA": {".tga"},
    "PSD": {".psd"},
}


@rule
def r_texture_wrong_extension(f: SkpFacts) -> Finding | None:
    hits = []
    for t in f.textures:
        if not t.fmt:
            continue
        ext = "." + t.filename.rsplit(".", 1)[-1].lower() if "." in t.filename else ""
        allowed = _EXPECTED_EXT.get(t.fmt)
        if allowed and ext not in allowed:
            hits.append((t, ext))
    if not hits:
        return None

    hits.sort(key=lambda pair: -pair[0].bytes)
    return Finding(
        id="textures.wrong_extension",
        severity="medium",
        category="текстуры",
        title=f"{len(hits)} текстур с неверным расширением",
        summary=(
            "Внутри файла лежит не тот формат, который заявлен в имени. "
            "SketchUp разбирает такую текстуру по расширению, спотыкается и "
            "переключается на угадывание — в логах это видно как ошибки "
            "декодера.\n\n"
            "Само по себе не смертельно, но часть плагинов и экспортёров на "
            "таких файлах отваливается молча."
        ),
        items=[
            f"{t.material}/{t.filename} — внутри {t.fmt}, а расширение {ext or 'отсутствует'}"
            for t, ext in hits
        ],
        count=len(hits),
        fix_kind="sdk",
        fix_note="Переименование рвёт ссылку из model.dat — правится только через SDK.",
    )


@rule
def r_texture_unreadable(f: SkpFacts) -> Finding | None:
    bad = [t for t in f.textures if t.unreadable]
    if not bad:
        return None
    return Finding(
        id="textures.unreadable",
        severity="medium",
        category="текстуры",
        title=f"{len(bad)} текстур не читаются",
        summary=(
            "Файл лежит в контейнере, но не открывается как изображение. Либо "
            "экзотический формат, либо битые данные — в SketchUp такая текстура "
            "покажется пустой."
        ),
        items=[f"{t.material}/{t.filename} — {t.unreadable}" for t in bad],
        count=len(bad),
        fix_kind="manual",
    )


def _estimate_downscale_saving(t: TextureInfo) -> int:
    """Прикинуть, сколько байт освободит даунскейл до MAX_TEXTURE_DIM.

    Оценка грубая: вес сжатого изображения падает примерно пропорционально
    площади. Реальный результат считается уже при применении фикса.
    """
    if not t.width or not t.height:
        return 0
    longest = max(t.width, t.height)
    if longest <= MAX_TEXTURE_DIM:
        return 0
    scale = MAX_TEXTURE_DIM / longest
    return max(0, int(t.bytes * (1 - scale * scale)))


# Импорт в конце файла: rules_model регистрирует свои правила в RULES через
# декоратор @rule и для этого сам импортирует из этого модуля. К моменту
# импорта reestr и декоратор уже определены, цикла не возникает.
from . import rules_model  # noqa: E402,F401  (side-effect import)
