"""Тесты слоя SketchUp SDK.

Сам SDK в CI недоступен (он лицензионный и существует только под
Windows/macOS), поэтому здесь проверяется два свойства: что без SDK ничего
не падает, и что правила по геометрии считают правильно на подставных данных.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from atata.rules import analyze
from atata.sdk import SdkUnavailable, inspect_model, sdk_status
from atata.sdk.inspect import DefinitionInfo, ModelFacts
from atata.skp.facts import collect_facts


def test_status_never_raises():
    status = sdk_status()
    assert isinstance(status.available, bool)
    if not status.available:
        assert status.reason


def test_inspect_raises_cleanly_without_sdk(simple_skp: Path):
    if sdk_status().available:
        pytest.skip("SDK установлен — этот тест про его отсутствие")
    with pytest.raises(SdkUnavailable):
        inspect_model(simple_skp)


def test_analysis_works_without_sdk(simple_skp: Path):
    facts = collect_facts(simple_skp)
    assert facts.model is None
    findings = analyze(facts)
    assert not any(f.layer == "model" for f in findings)
    assert not any(f.id.startswith("rule-error") for f in findings)


# ---------------------------------------------------------------- геометрия


def fake_model(**kwargs) -> ModelFacts:
    defaults = dict(
        path="fake.skp",
        direct_faces=200,
        direct_edges=600,
        total_faces=1000,
        total_edges=3000,
    )
    defaults.update(kwargs)
    return ModelFacts(**defaults)


def analyse_with_model(skp: Path, model: ModelFacts):
    facts = collect_facts(skp)
    facts.model = model
    return {f.id: f for f in analyze(facts)}


def test_heavy_scene_flagged(simple_skp: Path):
    found = analyse_with_model(simple_skp, fake_model(total_faces=3_000_000))
    assert found["geometry.faces"].severity == "critical"


def test_moderate_scene_is_high_not_critical(simple_skp: Path):
    found = analyse_with_model(simple_skp, fake_model(total_faces=800_000))
    assert found["geometry.faces"].severity == "high"


def test_light_scene_not_flagged(simple_skp: Path):
    found = analyse_with_model(simple_skp, fake_model(total_faces=10_000))
    assert "geometry.faces" not in found


def test_unused_definitions_flagged(simple_skp: Path):
    model = fake_model(
        definitions=[
            DefinitionInfo("Стул", 5000, 12000, 5000, instances=3, used_instances=3),
            DefinitionInfo("Мусор", 40000, 90000, 40000, instances=0, used_instances=0),
        ]
    )
    found = analyse_with_model(simple_skp, model)
    unused = found["geometry.unused_definitions"]
    assert unused.count == 1
    assert "Мусор" in unused.items[0]
    assert "Стул" not in " ".join(unused.items)


def test_heavy_components_multiply_by_instances(simple_skp: Path):
    """Вклад определения считается с учётом числа вставок, а не один раз."""
    model = fake_model(
        definitions=[
            DefinitionInfo("Фикус", 30_000, 60_000, 30_000, instances=8, used_instances=8),
        ]
    )
    found = analyse_with_model(simple_skp, model)
    heavy = found["geometry.heavy_components"]
    assert "240 000 граней" in heavy.items[0]
    assert "×8" in heavy.items[0]


def test_nesting_flagged(simple_skp: Path):
    found = analyse_with_model(simple_skp, fake_model(max_depth=12))
    assert found["geometry.nesting"].count == 12
    assert "geometry.nesting" not in analyse_with_model(simple_skp, fake_model(max_depth=3))


def test_loose_edges_flagged(simple_skp: Path):
    """Правило обязано называть то число, которое исправление и уберёт."""
    found = analyse_with_model(
        simple_skp,
        fake_model(loose_edges=3457, loose_edges_total=500_000, loose_edges_actionable=450_429),
    )
    finding = found["geometry.loose_edges"]
    assert "450 429" in finding.title
    assert finding.count == 450_429
    assert "3 457" in finding.summary, "в корне — отдельным числом"


def test_linework_containers_are_called_out(simple_skp: Path):
    """Компоненты из одних линий защищены, и об этом надо сказать."""
    found = analyse_with_model(
        simple_skp,
        fake_model(
            loose_edges=100,
            loose_edges_total=500_000,
            loose_edges_actionable=450_000,
            linework_containers=12,
        ),
    )
    summary = found["geometry.loose_edges"].summary
    assert "50 000" in summary
    assert "12" in summary


def test_loose_edges_not_flagged_when_only_linework(simple_skp: Path):
    """Если убирать нечего, находки быть не должно, сколько бы линий ни лежало."""
    found = analyse_with_model(
        simple_skp,
        fake_model(loose_edges_total=500_000, loose_edges_actionable=0, linework_containers=30),
    )
    assert "geometry.loose_edges" not in found


def test_truncated_walk_reported(simple_skp: Path):
    found = analyse_with_model(simple_skp, fake_model(truncated=True))
    assert found["geometry.truncated"].severity == "info"


def test_container_estimate_yields_to_real_data(simple_skp: Path, tmp_path: Path):
    """Пока SDK нет — косвенная оценка веса; когда есть — она уходит."""
    from .conftest import make_skp

    big = make_skp(tmp_path / "big.skp", model_bytes=300 * 1024 * 1024)

    without = {f.id for f in analyze(collect_facts(big))}
    assert "model.weight" in without

    facts = collect_facts(big)
    facts.model = fake_model()
    with_sdk = {f.id for f in analyze(facts)}
    assert "model.weight" not in with_sdk


# ---------------------------------------------------------------- запуск воркера


def test_public_api_is_importable():
    """Всё, что fixes.py тянет из пакета, должно из него импортироваться.

    Ловит класс ошибок, который тесты пропускают: функцию добавили в runner,
    но забыли в __init__, и падает оно уже в бою на первой же задаче.
    """
    import atata.sdk as sdk

    for name in ("can_open", "repair_geometry", "analyze_geometry", "detect"):
        assert hasattr(sdk, name), f"{name} не экспортирован из atata.sdk"
        assert name in sdk.__all__


def test_fixes_imports_resolve(simple_skp: Path, tmp_path: Path):
    """Путь применения фикса должен доходить до вызова SDK, а не падать на импорте."""
    from atata.fixes import apply_fixes
    from atata.skp.facts import collect_facts

    facts = collect_facts(simple_skp)
    report = apply_fixes(simple_skp, tmp_path / "out.skp", ["purge_unused"], facts)
    # Результат зависит от наличия SDK, но ImportError быть не должно.
    assert not any("ImportError" in e for e in report.errors), report.errors


def test_detect_reports_mode():
    from atata.sdk import detect

    config = detect()
    assert config.mode in ("native", "wine", "disabled")
    if config.mode == "disabled":
        assert config.reason, "отказ обязан объяснять причину"
    else:
        assert config.command


def test_every_serialized_field_is_read_back():
    """Поле, попавшее в JSON, должно читаться обратно.

    Ловит перекос, который тесты иначе пропускают: поле добавили в as_dict,
    а в from_payload забыли — правило молча перестаёт срабатывать.
    """
    from atata.sdk.runner import from_payload

    source = fake_model(
        loose_edges=3457,
        loose_edges_total=500_000,
        loose_edges_actionable=450_429,
        linework_containers=12,
        scenes=6,
        styles=5,
        max_depth=11,
        truncated=True,
    )
    payload = source.as_dict()
    payload["materials_list"] = source.materials
    payload["layers_list"] = source.layers
    payload["definitions_detail"] = []

    restored = from_payload(payload, "x.skp")
    skip = {"definitions", "materials", "layers", "heaviest", "unused_definitions"}
    for key, value in source.as_dict().items():
        if key in skip:
            continue
        assert getattr(restored, key, None) == value, f"поле {key} потерялось"


def test_payload_roundtrip():
    """JSON воркера должен разворачиваться обратно без потерь."""
    from atata.sdk.runner import from_payload

    source = fake_model(
        total_faces=4_545_548,
        total_edges=9_728_801,
        max_depth=11,
        loose_edges=3457,
        definitions=[
            DefinitionInfo("Фикус", 30_000, 0, 30_000, instances=8, used_instances=8),
            DefinitionInfo("Мусор", 500, 0, 500, instances=0, used_instances=0),
        ],
        materials=["a", "b"],
        layers=["Layer0"],
        scenes=6,
    )

    payload = source.as_dict()
    payload["definitions_detail"] = [
        {
            "name": d.name,
            "own_faces": d.own_faces,
            "expanded_faces": d.expanded_faces,
            "instances": d.instances,
            "used_instances": d.used_instances,
        }
        for d in source.definitions
    ]
    payload["materials_list"] = source.materials
    payload["layers_list"] = source.layers

    restored = from_payload(payload, "x.skp")
    assert restored.total_faces == source.total_faces
    assert restored.total_edges == source.total_edges
    assert restored.max_depth == 11
    assert restored.loose_edges == 3457
    assert len(restored.definitions) == 2
    assert len(restored.unused_definitions) == 1
    assert restored.heaviest(1)[0].total_faces == 240_000
    assert restored.materials == ["a", "b"]


def test_worker_reports_missing_argument(tmp_path: Path):
    from atata.sdk.worker import main

    assert main([]) == 2


def test_worker_rejects_unknown_command():
    from atata.sdk.worker import main

    assert main(["polish"]) == 2


def test_repair_report_shape():
    """Отчёт о правках должен доезжать до фронта целиком."""
    from atata.sdk.repair import RepairReport

    report = RepairReport(
        operations=["fix_errors", "purge_unused"],
        definitions_before=3820,
        definitions_after=1573,
        removed_definitions=2247,
        erased_edges=3457,
        textures_seen=345,
        textures_resized=280,
        passes=10,
        size_before=365_581_605,
        size_after=255_800_000,
        scenes=6,
        cleared_scene_hidden=653,
    )
    data = report.as_dict()
    assert data["saved"] == report.size_before - report.size_after
    for key in (
        "operations",
        "fix_errors_ran",
        "erased_edges",
        "textures_seen",
        "textures_resized",
        "textures_failed",
        "texture_scale_kept",
        "removed_definitions",
        "definitions_before",
        "definitions_after",
        "passes",
        "scenes",
        "cleared_scene_hidden",
        "errors",
    ):
        assert key in data


def test_scene_hidden_flags_are_accounted_for():
    """Каждая снятая пометка «скрыто» должна быть либо возвращена, либо объяснена."""
    from atata.sdk.repair import RepairReport

    report = RepairReport(
        cleared_scene_hidden=653,
        restored_scene_hidden=629,
        lost_scene_hidden=24,
    )
    data = report.as_dict()
    assert (
        data["restored_scene_hidden"] + data["lost_scene_hidden"]
        == data["cleared_scene_hidden"]
    ), "пометки не должны пропадать бесследно"


def test_operation_order_is_fixed():
    """Порядок операций осмыслен: чинить -> мусор -> текстуры -> purge."""
    from atata.sdk.repair import OPERATION_ORDER
    from atata.fixes import AVAILABLE_FIXES

    assert OPERATION_ORDER.index("fix_errors") < OPERATION_ORDER.index("purge_unused")
    assert OPERATION_ORDER.index("erase_loose_edges") < OPERATION_ORDER.index("purge_unused")
    # Каждая доступная правка должна быть известна конвейеру, иначе она
    # молча ничего не сделает.
    assert set(AVAILABLE_FIXES) == set(OPERATION_ORDER)


def test_next_power_of_two():
    from atata.sdk.repair import _next_pot

    assert _next_pot(4000, 2048) == 2048
    assert _next_pot(2250, 2048) == 2048
    assert _next_pot(1024, 2048) == 1024
    assert _next_pot(1000, 2048) == 512
    assert _next_pot(3, 2048) == 2
    assert _next_pot(0, 2048) == 1


# ---------------------------------------------------------------- имена


def test_mojibake_repaired():
    """Кириллица, прочитанная как cp1251 и сохранённая обратно в UTF-8."""
    from atata.sdk.inspect import fix_mojibake

    assert fix_mojibake("РљРѕРјРїРѕРЅРµРЅС‚#3") == "Компонент#3"


def test_mojibake_leaves_normal_names_alone():
    from atata.sdk.inspect import fix_mojibake

    for name in ("Компонент#79", "HunterDouglas_Esfera Cordao", "skp478C", ""):
        assert fix_mojibake(name) == name
