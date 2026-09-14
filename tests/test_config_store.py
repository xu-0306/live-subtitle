from __future__ import annotations

import multiprocessing
from pathlib import Path

import pytest

import backend.config_store as config_store
from backend.config_store import RevisionConflictError, load_snapshot, save_config


def _save_from_process(path: str, expected: int, value: str, barrier, result_queue) -> None:
    """Top-level worker so the contention test also runs with Windows spawn."""

    barrier.wait(timeout=10)
    try:
        revision = save_config(path, {"value": value}, expected_revision=expected)
    except RevisionConflictError as exc:
        result_queue.put(("conflict", exc.actual))
    else:
        result_queue.put(("saved", revision))


def test_yaml_snapshot_revisions_are_atomic_and_conflicts_are_explicit(tmp_path: Path) -> None:
    path = tmp_path / "settings.yaml"
    first = save_config(path, {"translation": {"target_language": "粵語"}})
    assert first == 1
    snapshot, revision = load_snapshot(path)
    assert snapshot["translation"]["target_language"] == "粵語"
    assert revision == first

    with pytest.raises(RevisionConflictError) as error:
        save_config(path, {"stale": True}, expected_revision=revision - 1)
    assert error.value.expected == 0
    assert error.value.actual == revision
    latest, latest_revision = load_snapshot(path)
    assert latest == snapshot
    assert latest_revision == revision


def test_revision_check_serializes_writers_in_separate_processes(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    initial = save_config(path, {"value": "initial"})
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    result_queue = context.Queue()
    processes = [
        context.Process(
            target=_save_from_process,
            args=(str(path), initial, value, barrier, result_queue),
        )
        for value in ("first", "second")
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(20)
        assert process.exitcode == 0
    results = [result_queue.get(timeout=5) for _ in processes]
    assert sorted(result[0] for result in results) == ["conflict", "saved"]
    document, revision = load_snapshot(path)
    assert document["value"] in {"first", "second"}
    assert revision == initial + 1


def test_external_edit_cannot_reuse_a_stale_initial_revision(tmp_path: Path) -> None:
    path = tmp_path / "settings.yaml"
    initial = save_config(path, {"value": "first"})
    # Simulate a legacy writer or an interrupted sidecar publication.  The
    # embedded marker and content-derived fallback must make this a new
    # revision identity, rather than accepting an old expected value.
    path.write_text("value: externally-edited\n", encoding="utf-8")
    _, external_revision = load_snapshot(path)
    assert external_revision not in {0, initial}
    with pytest.raises(RevisionConflictError):
        save_config(path, {"value": "stale overwrite"}, expected_revision=initial)


def test_embedded_revision_survives_missing_sidecar(tmp_path: Path) -> None:
    path = tmp_path / "settings.yaml"
    revision = save_config(path, {"value": "committed"})
    path.with_name(path.name + ".revision").unlink()

    document, loaded_revision = load_snapshot(path)
    assert document["value"] == "committed"
    assert loaded_revision == revision
    with pytest.raises(RevisionConflictError):
        save_config(path, {"value": "stale"}, expected_revision=0)


def test_interrupted_sidecar_write_cannot_accept_stale_initial_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "settings.yaml"

    def fail_sidecar(*args, **kwargs):
        raise OSError("simulated sidecar failure")

    monkeypatch.setattr(config_store, "_write_revision", fail_sidecar)
    with pytest.raises(OSError):
        save_config(path, {"value": "committed"})

    _, revision = load_snapshot(path)
    assert revision > 0
    with pytest.raises(RevisionConflictError):
        save_config(path, {"value": "stale"}, expected_revision=0)


def test_json_revision_roundtrip_stays_within_javascript_safe_integer(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    save_config(path, {"value": "first"})
    path.write_text('{"value":"external","unicode":"測試"}\n', encoding="utf-8")
    document, revision = load_snapshot(path)
    assert document["unicode"] == "測試"
    assert 0 < revision < 2**53
    assert int(str(revision)) == revision
