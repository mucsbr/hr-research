"""Filesystem fault injection, with no Docker or candidate execution."""

import hashlib
import io
from pathlib import Path
import sys
import tarfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import RunnerError, digest
from resolver import Resolver
from storage import ArtifactStore, QUOTAS, inspect_install_archive
from test_runner import Broker, FakeBackend, FakeResolver, TOKEN, bundle


def unseal(root):
    for path in root.rglob("*"):
        if not path.is_symlink():
            path.chmod(0o755 if path.is_dir() else 0o644)


def test_wheel_write_is_atomic_and_repairs_known_hash(tmp_path, monkeypatch):
    store = ArtifactStore(tmp_path, TOKEN)
    data = b"approved fixture bytes"
    expected = hashlib.sha256(data).hexdigest()
    broken = tmp_path / "wheels" / expected
    broken.write_bytes(b"partial download")
    store.write_wheel(expected, data)
    assert broken.read_bytes() == data
    assert list((tmp_path / "quarantine").iterdir())
    import storage

    original = storage.os.replace

    def interrupted(source, target):
        if target.name == hashlib.sha256(b"next bytes").hexdigest():
            raise OSError("simulated interrupted rename")
        return original(source, target)

    monkeypatch.setattr(storage.os, "replace", interrupted)
    with pytest.raises(OSError):
        store.write_wheel(hashlib.sha256(b"next bytes").hexdigest(), b"next bytes")
    assert not list((tmp_path / "wheels").glob(".tmp-*"))
    monkeypatch.setattr(storage.os, "replace", original)
    store.write_wheel(hashlib.sha256(b"next bytes").hexdigest(), b"next bytes")


def test_cache_quota_rejects_before_file_allocation(tmp_path, monkeypatch):
    store = ArtifactStore(tmp_path, TOKEN)
    monkeypatch.setitem(QUOTAS, "wheels", 1)
    with pytest.raises(RunnerError, match="cache_quota_exceeded"):
        store.write_wheel(hashlib.sha256(b"x").hexdigest(), b"x")
    assert not list((tmp_path / "wheels").iterdir())
    monkeypatch.setitem(QUOTAS, "bundles", 1)
    with pytest.raises(RunnerError, match="cache_quota_exceeded"):
        with store.staging("bundles", 4096):
            raise AssertionError("must reject before creating directory")
    assert not list((tmp_path / "bundles").iterdir())


def test_authenticated_orphans_reconcile_without_reinstall(tmp_path):
    broker = Broker(tmp_path, TOKEN, FakeBackend(), FakeResolver)
    try:
        value = bundle(broker)
        with broker.db() as db:
            db.execute("DELETE FROM bundles")
            db.execute("DELETE FROM locks")
        broker.recover()
        assert broker.put_bundle(digest(value), value)["created"] is False
        resolved = broker.resolve({"dependencies": []})
        assert resolved["lock"] == value["lock"]
        with broker.db() as db:
            assert db.execute("SELECT COUNT(*) FROM locks").fetchone()[0] == 1
            assert db.execute("SELECT COUNT(*) FROM bundles").fetchone()[0] == 1
    finally:
        broker.executor.shutdown()
        unseal(tmp_path)


def test_corrupted_orphan_is_quarantined_and_same_sha_retry_works(tmp_path):
    broker = Broker(tmp_path, TOKEN, FakeBackend(), FakeResolver)
    try:
        value = bundle(broker)
        target = tmp_path / "bundles" / digest(value) / "adapter.py"
        target.chmod(0o644)
        target.write_text("damaged partial source")
        with broker.db() as db:
            db.execute("DELETE FROM bundles")
        broker.put_bundle(digest(value), value)
        assert target.read_text() == value["files"]["adapter.py"]
        assert list((tmp_path / "quarantine").iterdir())
    finally:
        broker.executor.shutdown()
        unseal(tmp_path)


@pytest.mark.parametrize(
    "name,kind",
    [
        ("../outside", tarfile.REGTYPE),
        ("/outside", tarfile.REGTYPE),
        ("link", tarfile.SYMTYPE),
        ("a//b", tarfile.REGTYPE),
    ],
)
def test_dependency_export_rejects_paths_and_links(name, kind):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        entry = tarfile.TarInfo(name)
        entry.type = kind
        entry.linkname = "/etc/passwd" if kind == tarfile.SYMTYPE else ""
        archive.addfile(entry)
    with pytest.raises(RunnerError, match="unsafe_dependency_export"):
        inspect_install_archive(output.getvalue())


def test_combined_wheel_expansion_checked_before_install(tmp_path):
    resolver = Resolver(tmp_path)

    def wheel(name, version):
        key = hashlib.sha256(name.encode()).hexdigest()
        resolver.wheel_sizes[key] = (25_000_000, 1501)
        return (
            {"name": name, "version": str(version), "filename": name + ".whl", "sha256": key},
            [],
        )

    resolver.wheel = wheel
    with pytest.raises(RunnerError, match="combined_wheel_expansion_budget"):
        resolver.resolve(["six==1.17.0", "packaging==25.0"])


def test_failed_preparation_and_interrupted_builds_are_cleaned(tmp_path, monkeypatch):
    broker = Broker(tmp_path, TOKEN, FakeBackend(), FakeResolver)
    try:

        def fail(directory, lock):
            directory.mkdir(parents=True)
            (directory / "partial").write_text("partial preparation")
            raise OSError("injected preparation failure")

        monkeypatch.setattr(broker, "prepare_wheelhouse", fail)
        with pytest.raises(OSError):
            broker.resolve({"dependencies": []})
        assert not list((tmp_path / "builds").iterdir())
        stale = tmp_path / "builds" / ("a" * 32)
        stale.mkdir()
        (stale / "partial").write_text("stale controlled build")
        unrelated = tmp_path / "builds" / "unrelated-operator-file"
        unrelated.write_text("keep")
        broker.recover()
        assert not stale.exists() and unrelated.read_text() == "keep"
    finally:
        broker.executor.shutdown()
        unseal(tmp_path)


def test_quarantine_accounts_for_small_file_allocations(tmp_path, monkeypatch):
    store = ArtifactStore(tmp_path, TOKEN)
    directory = tmp_path / "bundles" / ("b" * 64)
    directory.mkdir()
    for number in range(10):
        (directory / str(number)).write_text("x")
    monkeypatch.setitem(QUOTAS, "quarantine", 16_384)
    store.quarantine(directory, "controlled small-file quota case")
    assert store.usage("quarantine") <= 16_384
    assert not directory.exists()
