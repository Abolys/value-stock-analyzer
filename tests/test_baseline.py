"""Seeding the runtime databases from the committed baseline/ snapshot (storage/baseline.py)."""

from __future__ import annotations

import os
import stat

import pytest

# Bound at collection, before the autouse fixture stubs the module attribute for the app tests.
from storage.baseline import _replace_with_writable_copy, bootstrap_baseline


def test_copies_missing_files_only(tmp_path):
    base = tmp_path / "baseline"
    base.mkdir()
    (base / "a.db").write_bytes(b"baseline-a")
    (base / "b.db").write_bytes(b"baseline-b")
    live_a, live_b = tmp_path / "live" / "a.db", tmp_path / "live" / "b.db"
    live_b.parent.mkdir()
    live_b.write_bytes(b"local-b")
    copied = bootstrap_baseline(baseline_dir=base, pairs=[("a.db", live_a), ("b.db", live_b)])
    assert copied == ["a.db"]
    assert live_a.read_bytes() == b"baseline-a"
    assert live_b.read_bytes() == b"local-b"  # local data wins


def test_new_baseline_replaces_seeded_copy(tmp_path):
    base = tmp_path / "baseline"
    base.mkdir()
    (base / "a.db").write_bytes(b"v1")
    live = tmp_path / "a.db"
    assert bootstrap_baseline(baseline_dir=base, pairs=[("a.db", live)]) == ["a.db"]
    live.write_bytes(b"v1 + site writes")
    assert bootstrap_baseline(baseline_dir=base, pairs=[("a.db", live)]) == []  # same baseline: site data kept
    assert live.read_bytes() == b"v1 + site writes"
    (base / "a.db").write_bytes(b"v2")  # a new baseline was pushed
    assert bootstrap_baseline(baseline_dir=base, pairs=[("a.db", live)]) == ["a.db"]
    assert live.read_bytes() == b"v2"


def test_unseeded_local_file_is_never_replaced(tmp_path):
    """The machine that builds the baseline has live files and no marker: its data wins."""
    base = tmp_path / "baseline"
    base.mkdir()
    (base / "a.db").write_bytes(b"older snapshot")
    live = tmp_path / "a.db"
    live.write_bytes(b"newer local")
    assert bootstrap_baseline(baseline_dir=base, pairs=[("a.db", live)]) == []
    assert live.read_bytes() == b"newer local"


def test_missing_baseline_is_a_noop(tmp_path):
    assert bootstrap_baseline(baseline_dir=tmp_path / "none", pairs=[("a.db", tmp_path / "a.db")]) == []
    assert not (tmp_path / "a.db").exists()


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="needs file permissions enforced")
def test_read_only_live_file_becomes_writable(tmp_path):
    live = tmp_path / "a.db"
    live.write_bytes(b"checked-out")
    live.chmod(stat.S_IRUSR)
    copied = bootstrap_baseline(baseline_dir=tmp_path / "none", pairs=[("a.db", live)])
    assert copied == ["a.db"]
    assert os.access(live, os.W_OK)
    assert live.read_bytes() == b"checked-out"  # its own content, not the baseline's


def test_replace_keeps_content(tmp_path):
    f = tmp_path / "x.db"
    f.write_bytes(b"data")
    _replace_with_writable_copy(f)
    assert f.read_bytes() == b"data" and not (tmp_path / "x.db.tmp").exists()
