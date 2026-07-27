"""Тесты контейнера: разбор заголовка, пересборка, целостность."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from atata.skp import meta as meta_mod
from atata.skp.container import NotASkpFile, SkpContainer, verify

from .conftest import build_meta, make_skp


def test_prefix_and_version_parsed(simple_skp: Path):
    with SkpContainer(simple_skp) as skp:
        assert skp.version == "24.0.484"
        assert "SketchUp Model" in skp.header_strings
        # Заголовок должен заканчиваться ровно перед сигнатурой ZIP.
        assert skp.prefix.endswith(b"\xb5\x16")
        assert len(skp.prefix) == 69


def test_entries_listed(simple_skp: Path):
    with SkpContainer(simple_skp) as skp:
        names = {e.name for e in skp.entries}
    assert "model.dat" in names
    assert "meta/meta.dat" in names
    assert "materials/Wood/wood.jpg" in names


def test_verify_accepts_valid_file(simple_skp: Path):
    ok, message = verify(simple_skp)
    assert ok, message
    assert "24.0.484" in message


def test_plain_zip_is_rejected(tmp_path: Path):
    """ZIP без префикса SketchUp не должен приниматься за .skp."""
    path = tmp_path / "plain.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("model.dat", b"nope")

    # Префикса нет: контейнер прочитается, но проверка обязана заметить
    # отсутствие строки версии в заголовке.
    ok, message = verify(path)
    assert not ok
    assert "версии" in message


def test_garbage_is_rejected(tmp_path: Path):
    path = tmp_path / "garbage.skp"
    path.write_bytes(b"\x00" * 5000)
    with pytest.raises(NotASkpFile):
        SkpContainer(path)


def test_rebuild_preserves_everything(simple_skp: Path, tmp_path: Path):
    dest = tmp_path / "rebuilt.skp"
    with SkpContainer(simple_skp) as skp:
        before = {e.name: e.size for e in skp.entries}
        prefix = skp.prefix
        result = skp.rebuild(dest)

    assert result.dropped == []
    assert result.changed == []

    with SkpContainer(dest) as rebuilt:
        assert rebuilt.prefix == prefix
        assert {e.name: e.size for e in rebuilt.entries} == before
        assert rebuilt.version == "24.0.484"

    ok, message = verify(dest)
    assert ok, message


def test_rebuild_keeps_payload_identical(simple_skp: Path, tmp_path: Path):
    dest = tmp_path / "rebuilt.skp"
    with SkpContainer(simple_skp) as skp:
        skp.rebuild(dest)
        original = {e.name: skp.read(e.name) for e in skp.entries}

    with SkpContainer(dest) as rebuilt:
        for name, payload in original.items():
            assert rebuilt.read(name) == payload, name


def test_rebuild_applies_transform_only_to_selected(simple_skp: Path, tmp_path: Path):
    dest = tmp_path / "patched.skp"
    seen: list[str] = []

    def transform(entry, data):
        seen.append(entry.name)
        return b"PATCHED"

    with SkpContainer(simple_skp) as skp:
        skp.rebuild(
            dest,
            selector=lambda e: e.name == "materials/Wood/wood.jpg",
            transform=transform,
        )

    # transform не должен даже вызываться для невыбранных записей —
    # иначе model.dat снова окажется в памяти целиком.
    assert seen == ["materials/Wood/wood.jpg"]

    with SkpContainer(dest) as patched:
        assert patched.read("materials/Wood/wood.jpg") == b"PATCHED"
        assert patched.read("model.dat").startswith(b"GEOMETRY-BLOCK-")


def test_rebuild_can_drop_entries(simple_skp: Path, tmp_path: Path):
    dest = tmp_path / "dropped.skp"
    with SkpContainer(simple_skp) as skp:
        result = skp.rebuild(dest, drop=lambda e: e.name.startswith("materials/Glass"))

    assert result.dropped == ["materials/Glass/material.xml"]
    with SkpContainer(dest) as after:
        assert "materials/Glass/material.xml" not in {e.name for e in after.entries}


def test_meta_parsed(simple_skp: Path):
    with SkpContainer(simple_skp) as skp:
        parsed = meta_mod.parse(skp.read("meta/meta.dat"))

    assert parsed.version_string == "24.0.484"
    assert parsed.version_major == 24
    assert parsed.units == "Millimeter"
    assert parsed.source_path.startswith("\\\\office")


def test_meta_survives_truncation():
    """Обрезанный блок не должен ронять разбор — только терять хвост."""
    raw = build_meta("24.0.484", "Millimeter", "C:/x.skp")
    parsed = meta_mod.parse(raw[: len(raw) - 5])
    assert parsed.version_string == "24.0.484"
    assert parsed.source_path is None


def test_version_from_header_when_meta_missing(tmp_path: Path):
    path = make_skp(tmp_path / "v.skp", version="23.1.340")
    with SkpContainer(path) as skp:
        assert skp.version == "23.1.340"
