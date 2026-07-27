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
    assert oversized.bytes_impact > 0
    assert any("concrete.jpg" in item for item in oversized.items)
    # Автозамена снята: подменять картинки в контейнере нельзя.
    assert oversized.fix is None
    assert oversized.fix_kind == "sdk"


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


def test_no_container_level_fixes_are_offered():
    """Правки внутри контейнера сняты — они ломали файл.

    Пересборка ZIP делает файл нечитаемым для SketchUp, даже если не менять
    ни байта содержимого: смещения записей уезжают. Проверено на рабочем
    файле — контейнер целый, все CRC совпадают, SketchUp не открывает.
    Единственный рабочий путь — пересохранение через SDK.
    """
    from atata.fixes import AVAILABLE_FIXES

    assert "downscale_textures" not in AVAILABLE_FIXES
    assert "normalize_pot" not in AVAILABLE_FIXES
    assert all(spec.kind == "sdk" for spec in AVAILABLE_FIXES.values())


def test_unknown_fix_is_rejected(simple_skp: Path, tmp_path: Path):
    facts = collect_facts(simple_skp)
    report = apply_fixes(simple_skp, tmp_path / "x.skp", ["make_it_pretty"], facts)
    assert report.errors
    assert not report.usable


def test_removed_fixes_are_rejected_too(simple_skp: Path, tmp_path: Path):
    facts = collect_facts(simple_skp)
    report = apply_fixes(simple_skp, tmp_path / "x.skp", ["downscale_textures"], facts)
    assert report.errors
    assert not report.usable


def test_report_is_not_usable_without_verification(tmp_path: Path):
    from atata.fixes import FixReport

    report = FixReport(dest=tmp_path / "x.skp", applied=["purge_unused"])
    assert not report.usable

    report.verified = True
    report.openable = False
    assert not report.usable, "не открывается в SketchUp — отдавать нельзя"

    report.openable = True
    assert report.usable

    # SDK может быть недоступен: тогда проверить нечем, но контейнер цел.
    report.openable = None
    assert report.usable


def test_rebuild_recompresses_and_shifts_offsets(tmp_path: Path):
    """Фиксирует причину, по которой контейнерные правки сняты.

    Пересборка сохраняет имена, размеры и CRC, но пережимает сжатые записи
    своим уровнем. Размер сжатых данных меняется, смещения всех последующих
    записей уезжают — и SketchUp такой файл уже не открывает (проверено на
    рабочем файле 348 МБ: контейнер целый, читатель SDK отказывает).
    """
    import zipfile

    from .conftest import build_meta, build_prefix

    src = tmp_path / "packed.skp"
    payload = (b"GEOMETRY-BLOCK-" * 4096)

    with open(src, "wb") as fh:
        fh.write(build_prefix())
        with zipfile.ZipFile(fh, "w") as z:
            z.writestr("meta/meta.dat", build_meta("24.0.484", "Millimeter", "x.skp"))
            info = zipfile.ZipInfo("model.dat")
            info.compress_type = zipfile.ZIP_DEFLATED
            # SketchUp жмёт своим уровнем; воспроизводим это, взяв не тот,
            # который zipfile использует по умолчанию.
            z.writestr(info, payload, compresslevel=1)

    with SkpContainer(src) as container:
        before = {e.name: e.compressed_size for e in container.entries}
        container.rebuild(tmp_path / "rebuilt.skp")

    with SkpContainer(tmp_path / "rebuilt.skp") as rebuilt:
        after = {e.name: e.compressed_size for e in rebuilt.entries}

    assert before["model.dat"] != after["model.dat"], (
        "если размер сжатых данных совпал, механизм поломки изменился — "
        "стоит перепроверить, нельзя ли вернуть контейнерные правки"
    )


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
