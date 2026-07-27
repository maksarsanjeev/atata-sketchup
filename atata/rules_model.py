"""Правила по геометрии — работают, только когда доступен SketchUp SDK.

Модуль импортируется из :mod:`atata.rules`, поэтому регистрация в общем
реестре происходит автоматически. Если `SkpFacts.model` пуст (SDK не
подключён), каждое правило молча возвращает ``None``, и в отчёте остаются
косвенные оценки контейнерного слоя.
"""

from __future__ import annotations

from .rules import Finding, rule
from .skp.facts import SkpFacts

FACES_WARN = 500_000
FACES_CRIT = 2_000_000
NESTING_WARN = 8
LOOSE_EDGES_WARN = 500


def _model(f: SkpFacts):
    return f.model


@rule
def r_geometry_total(f: SkpFacts) -> Finding | None:
    model = _model(f)
    if model is None or model.total_faces < FACES_WARN:
        return None

    severity = "critical" if model.total_faces > FACES_CRIT else "high"
    return Finding(
        id="geometry.faces",
        severity=severity,
        category="геометрия",
        title=f"{model.total_faces:,} граней в сцене".replace(",", " "),
        summary=(
            f"Считано с раскрытием компонентов: определение, вставленное сорок раз, "
            f"даёт сорок наборов геометрии — именно столько обрабатывает вьюпорт.\n\n"
            f"Прямо в корне модели лежит всего {model.direct_faces:,} граней, "
            f"остальное приходит из групп и компонентов. Всего рёбер в сцене "
            f"{model.total_edges:,}. Для интерьерной сцены разумный потолок — "
            f"сотни тысяч граней; выше начинает проседать навигация."
        ).replace(",", " "),
        layer="model",
        count=model.total_faces,
        fix_kind="manual",
        fix_note="Лечится заменой тяжёлых моделей на упрощённые — см. список ниже.",
    )


@rule
def r_heavy_components(f: SkpFacts) -> Finding | None:
    model = _model(f)
    if model is None:
        return None
    heavy = [d for d in model.heaviest(30) if d.total_faces > 20_000]
    if not heavy:
        return None

    # Разделитель разрядов подставляем в число отдельно: .replace(",", " ")
    # по всей строке съедает запятые самого текста.
    worst = f"{heavy[0].total_faces:,}".replace(",", " ")
    return Finding(
        id="geometry.heavy_components",
        severity="high",
        category="геометрия",
        title=f"{len(heavy)} тяжёлых компонентов, худший — {worst} граней",
        summary=(
            "Самые тяжёлые определения в сцене. Обычно это мебель и растения из "
            "3D Warehouse, смоделированные под рендер крупным планом и вставленные "
            "в проект как есть.\n\n"
            "Замена такой модели на упрощённую или на 2D-фасад даёт больше, чем "
            "любая чистка материалов.\n\n"
            "Цифры не складывайте: вложенный компонент считается и сам по себе, "
            "и в составе родителя."
        ),
        layer="model",
        items=[
            f"{d.total_faces:,} граней · ×{d.used_instances} вставок · {d.name}".replace(",", " ")
            for d in heavy
        ],
        count=len(heavy),
        fix_kind="manual",
    )


@rule
def r_unused_definitions(f: SkpFacts) -> Finding | None:
    model = _model(f)
    if model is None:
        return None
    unused = model.unused_definitions
    if not unused:
        return None

    return Finding(
        id="geometry.unused_definitions",
        severity="high",
        category="геометрия",
        title=f"{len(unused)} определений компонентов не используются",
        summary=(
            "Эти компоненты лежат в файле, но ни разу не вставлены в модель. "
            "Они попадают туда после удаления объектов со сцены: сам объект ушёл, "
            "определение осталось и продолжает весить.\n\n"
            "Это ровно то, что убирает штатный Purge Unused."
        ),
        layer="model",
        items=[
            f"{d.own_faces:,} граней · {d.name}".replace(",", " ")
            for d in sorted(unused, key=lambda d: -d.own_faces)
        ],
        count=len(unused),
        fix="purge_unused",
        fix_kind="sdk",
        fix_label="Вычистить неиспользуемое",
    )


@rule
def r_nesting_depth(f: SkpFacts) -> Finding | None:
    model = _model(f)
    if model is None or model.max_depth < NESTING_WARN:
        return None

    return Finding(
        id="geometry.nesting",
        severity="medium",
        category="геометрия",
        title=f"Вложенность групп и компонентов — {model.max_depth} уровней",
        summary=(
            "Глубокая вложенность появляется, когда группы много раз оборачивают "
            "друг в друга при копировании. В работе это означает десяток двойных "
            "кликов, чтобы добраться до грани, и заметное замедление операций."
        ),
        layer="model",
        count=model.max_depth,
        fix_kind="manual",
    )


@rule
def r_loose_geometry(f: SkpFacts) -> Finding | None:
    model = _model(f)
    if model is None or model.loose_edges < LOOSE_EDGES_WARN:
        return None

    return Finding(
        id="geometry.loose_edges",
        severity="medium",
        category="геометрия",
        title=f"{model.loose_edges:,} висячих рёбер в корне модели".replace(",", " "),
        summary=(
            "Рёбра, не принадлежащие ни одной грани и не убранные в группу. Чаще "
            "всего это следы построения и остатки импорта DWG: на вид ничего, "
            "а при выделении рамкой цепляется всё подряд."
        ),
        layer="model",
        count=model.loose_edges,
        fix_kind="manual",
    )


@rule
def r_truncated_walk(f: SkpFacts) -> Finding | None:
    model = _model(f)
    if model is None or not model.truncated:
        return None

    return Finding(
        id="geometry.truncated",
        severity="info",
        category="служебное",
        title="Обход модели пришлось оборвать",
        summary=(
            "Достигнут предел вложенности или встретилась циклическая вставка "
            "компонента. Цифры по геометрии ниже реальных — учитывайте это."
        ),
        layer="model",
        fix_kind="manual",
    )
