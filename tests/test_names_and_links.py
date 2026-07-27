"""Тесты проверки имён и ссылок на внешние файлы."""

from __future__ import annotations

from pathlib import Path

from atata.names import check, repair_mojibake, script_of
from atata.rules import analyze
from atata.skp.facts import AssetLink, collect_facts


def codes(name: str) -> set[str]:
    return {issue.code for issue in check(name)}


# ---------------------------------------------------------------- имена


def test_clean_latin_name_has_no_issues():
    assert check("Wood_Floor_Light.jpg") == []


def test_non_latin_is_risky_not_broken():
    """Кириллица и иероглифы законны — валить их в «сломано» нельзя."""
    for name in ("Дерево_светлое.jpg", "赤木-2.JPG", "나무.png"):
        issues = check(name)
        assert issues, f"{name} должно хотя бы отмечаться"
        assert all(i.kind == "risky" for i in issues), name
        assert "non_latin" in codes(name)


def test_trailing_space_and_dot_are_broken():
    assert "trailing" in codes("vinyls .jpg")
    assert "trailing" in codes("logo rond..png")


def test_mojibake_detected_and_repaired():
    broken = "Дерево".encode("utf-8").decode("cp1251")
    assert repair_mojibake(broken) == "Дерево"
    issues = check(broken + ".jpg")
    assert any(i.code == "mojibake" and i.kind == "broken" for i in issues)


def test_mojibake_leaves_honest_names_alone():
    for name in ("Дерево.jpg", "wood.jpg", "赤木.jpg", ""):
        assert repair_mojibake(name) is None


def test_invisible_characters_detected():
    assert "invisible" in codes("wood\u200bfloor.jpg")


def test_illegal_and_reserved():
    assert "illegal" in codes('wood<floor>.jpg')
    assert "reserved" in codes("NUL.jpg")
    assert "reserved" in codes("com1.png")


def test_script_detection():
    assert script_of("wood") == "латиница"
    assert script_of("дерево") == "кириллица"
    assert script_of("wood_дерево") == "кириллица+латиница"
    assert script_of("12_-.") == "без букв"


# ---------------------------------------------------------------- ссылки


def link(path: str, material: str = "Mat") -> AssetLink:
    return AssetLink(material=material, plugin="BitmapBuffer", param="file", path=path)


def test_link_kind_classification():
    assert link(r"\\saga\projects\hdri\sky.hdr").kind == "unc"
    assert link(r"C:\Users\bob\Desktop\wood.jpg").kind == "local"
    assert link("wood.jpg").kind == "bare"


def test_link_filename_and_temp():
    assert link(r"C:\Users\bob\Downloads\wood.jpg").filename == "wood.jpg"
    assert link("C:/Users/x/AppData/Local/Temp/d1/wood.jpg").is_temp
    assert not link(r"\\saga\projects\wood.jpg").is_temp


def test_network_paths_are_not_reported_as_broken(simple_skp: Path):
    """Путь на сетевой ресурс может быть рабочим — из офиса он виден."""
    facts = collect_facts(simple_skp)
    facts.asset_links = [link(r"\\saga\projects\hdri\sky.hdr")]
    found = {f.id: f for f in analyze(facts)}
    finding = found["assets.dead_links"]
    assert finding.count == 0
    assert finding.severity == "info"
    assert "трогать не надо" in finding.summary


def test_local_paths_are_reported(simple_skp: Path):
    facts = collect_facts(simple_skp)
    facts.asset_links = [
        link(r"C:\Users\fpedrogo\My Documents\Textures\Speckled.jpg"),
        link(r"C:\Users\mason\Desktop\photo.jpg"),
        link(r"\\saga\projects\hdri\sky.hdr"),
    ]
    finding = {f.id: f for f in analyze(facts)}["assets.dead_links"]
    assert finding.count == 2
    assert finding.severity == "high"


def test_embedded_copy_is_noticed(simple_skp: Path):
    """У simple_skp материал Wood несёт wood.jpg — ссылку можно восстановить."""
    facts = collect_facts(simple_skp)
    facts.asset_links = [link(r"C:\Users\someone\Desktop\wood.jpg", material="Wood")]
    finding = {f.id: f for f in analyze(facts)}["assets.dead_links"]
    assert any("есть встроенная копия" in item for item in finding.items)
    assert finding.severity == "medium", "копия есть — значит не безнадёжно"


def test_no_links_no_finding(simple_skp: Path):
    facts = collect_facts(simple_skp)
    facts.asset_links = []
    assert "assets.dead_links" not in {f.id for f in analyze(facts)}
