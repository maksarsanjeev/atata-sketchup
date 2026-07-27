"""Тесты правил и применения исправлений."""

from __future__ import annotations

from pathlib import Path

from PIL import Image

from atata.fixes import apply_fixes
from atata.rules import MAX_TEXTURE_DIM, analyze
from atata.skp.container import SkpContainer
from atata.skp.facts import collect_facts, hamming


def findings_by_id(path: Path) -> dict:
    facts = collect_facts(path)
    return {f.id: f for f in analyze(facts)}


def test_clean_file_has_no_serious_findings(simple_skp: Path):
    found = findings_by_id(simple_skp)
    assert "textures.oversized" not in found
    assert "materials.count" not in found
    assert "textures.exact_duplicates" not in found


def test_oversized_textures_detected(messy_skp: Path):
    found = findings_by_id(messy_skp)
    oversized = found["textures.oversized"]
    assert oversized.severity == "high"
    assert oversized.fix == "downscale_textures"
    assert oversized.fix_kind == "auto"
    assert oversized.bytes_impact > 0
    assert any("concrete.jpg" in item for item in oversized.items)


def test_exact_duplicate_textures_detected(messy_skp: Path):
    found = findings_by_id(messy_skp)
    dupes = found["textures.exact_duplicates"]
    assert dupes.count == 1  # два материала делят один файл
    assert dupes.bytes_impact > 0


def test_auto_generated_material_names_detected(messy_skp: Path):
    found = findings_by_id(messy_skp)
    assert found["materials.auto"].count == 3


def test_material_families_detected(messy_skp: Path):
    found = findings_by_id(messy_skp)
    families = found["materials.families"]
    # Material1/2/3 и Image/Image2/Image3 — по семейству на каждую тройку.
    assert families.count >= 4


def test_extreme_aspect_detected(messy_skp: Path):
    found = findings_by_id(messy_skp)
    assert any("banner.jpg" in item for item in found["textures.aspect"].items)


def test_source_path_leak_detected(messy_skp: Path):
    found = findings_by_id(messy_skp)
    leak = found["privacy.source_path"]
    assert leak.items[0].startswith("\\\\office")


def test_rules_never_raise_on_empty_model(tmp_path: Path):
    from .conftest import make_skp

    path = make_skp(tmp_path / "bare.skp")
    facts = collect_facts(path)
    result = analyze(facts)
    assert not any(f.id.startswith("rule-error") for f in result)


# ---------------------------------------------------------------- фиксы


def test_downscale_shrinks_file_and_keeps_container_valid(messy_skp: Path, tmp_path: Path):
    dest = tmp_path / "fixed.skp"
    facts = collect_facts(messy_skp)
    report = apply_fixes(messy_skp, dest, ["downscale_textures"], facts)

    assert report.verified, report.verify_message
    assert report.saved > 0
    assert not report.errors
    assert report.size_after < report.size_before


def test_downscale_respects_dimension_limit(messy_skp: Path, tmp_path: Path):
    dest = tmp_path / "fixed.skp"
    facts = collect_facts(messy_skp)
    apply_fixes(messy_skp, dest, ["downscale_textures"], facts)

    after = collect_facts(dest)
    for texture in after.textures:
        assert max(texture.width, texture.height) <= MAX_TEXTURE_DIM, texture.entry


def test_downscale_leaves_small_textures_alone(messy_skp: Path, tmp_path: Path):
    dest = tmp_path / "fixed.skp"
    facts = collect_facts(messy_skp)
    report = apply_fixes(messy_skp, dest, ["downscale_textures"], facts)

    assert "materials/Wood/wood.jpg" not in report.touched

    with SkpContainer(messy_skp) as before, SkpContainer(dest) as after:
        assert before.read("materials/Wood/wood.jpg") == after.read("materials/Wood/wood.jpg")


def test_fix_never_touches_geometry(messy_skp: Path, tmp_path: Path):
    dest = tmp_path / "fixed.skp"
    facts = collect_facts(messy_skp)
    apply_fixes(messy_skp, dest, ["downscale_textures"], facts)

    with SkpContainer(messy_skp) as before, SkpContainer(dest) as after:
        assert before.read("model.dat") == after.read("model.dat")
        assert before.read("meta/meta.dat") == after.read("meta/meta.dat")
        assert before.prefix == after.prefix


def test_pot_normalisation(messy_skp: Path, tmp_path: Path):
    dest = tmp_path / "pot.skp"
    facts = collect_facts(messy_skp)
    apply_fixes(messy_skp, dest, ["downscale_textures", "normalize_pot"], facts)

    after = collect_facts(dest)
    for texture in after.textures:
        assert texture.width & (texture.width - 1) == 0, texture.entry
        assert texture.height & (texture.height - 1) == 0, texture.entry


def test_unknown_fix_is_rejected(simple_skp: Path, tmp_path: Path):
    facts = collect_facts(simple_skp)
    report = apply_fixes(simple_skp, tmp_path / "x.skp", ["make_it_pretty"], facts)
    assert report.errors
    assert not report.touched


# ---------------------------------------------------------------- хеши


def test_dhash_matches_recompressed_copy(tmp_path: Path):
    """Перцептивный хеш обязан пережить пересохранение с другим качеством."""
    from .conftest import make_image, make_skp

    original = make_image(256, 256, "JPEG")
    with Image.open(__import__("io").BytesIO(original)) as im:
        low = __import__("io").BytesIO()
        im.save(low, format="JPEG", quality=25)

    path = make_skp(
        tmp_path / "pair.skp",
        textures=[("A", "a.jpg", 256, 256, "JPEG")],
    )
    facts = collect_facts(path)
    assert facts.textures[0].dhash is not None


def test_hamming_distance():
    assert hamming(0b1010, 0b1010) == 0
    assert hamming(0b1010, 0b1011) == 1
    assert hamming(0, (1 << 64) - 1) == 64
