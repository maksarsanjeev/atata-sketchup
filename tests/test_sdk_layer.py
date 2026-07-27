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
        root_faces=1000,
        root_edges=3000,
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
    found = analyse_with_model(simple_skp, fake_model(loose_edges=4200))
    assert "4 200" in found["geometry.loose_edges"].title


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
