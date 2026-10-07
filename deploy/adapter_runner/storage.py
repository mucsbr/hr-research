"""Quota-bounded, authenticated artifact commits owned only by this Broker."""

from contextlib import contextmanager
import hashlib
import hmac
import io
import os
from pathlib import PurePosixPath
import re
import shutil
import tarfile
import threading
import uuid

from common import RunnerError, canonical, parse_json

MARKER = ".runner-manifest.json"
QUOTAS = {
    "wheels": 64_000_000,
    "dependencies": 256_000_000,
    "bundles": 32_000_000,
    "quarantine": 8_000_000,
}
INSTALL_BYTES = 64 * 1024 * 1024
INSTALL_FILES = 4096
INSTALL_ARCHIVE_BYTES = 72 * 1024 * 1024


def allocation_for_files(files):
    directories = {"."}
    total = 0
    for name, size in files.items():
        total += max(4096, ((size + 4095) // 4096) * 4096)
        directories.update(str(parent) for parent in PurePosixPath(name).parents)
    return total + 4096 * len(directories)


class ArtifactStore:
    def __init__(self, root, token):
        self.root, self.token, self.lock = root, token, threading.RLock()
        for category in QUOTAS:
            (root / category).mkdir(parents=True, exist_ok=True)

    def manifest(self, value):
        value = {key: item for key, item in value.items() if key != "signature"}
        key = hmac.new(
            self.token.encode(), b"hr-adapter-runner-artifact-v1", hashlib.sha256
        ).digest()
        value["signature"] = hmac.new(key, canonical(value), hashlib.sha256).hexdigest()
        return value

    def read_manifest(self, directory):
        path = directory / MARKER
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 512_000:
            raise RunnerError("artifact_manifest_invalid")
        value = parse_json(path.read_bytes(), 512_000)
        if not isinstance(value, dict) or not hmac.compare_digest(
            str(value.get("signature", "")), self.manifest(value)["signature"]
        ):
            raise RunnerError("artifact_manifest_invalid")
        return value

    def usage(self, category):
        total, count = 0, 0
        for path in (self.root / category).rglob("*"):
            count += 1
            if count > 300_000 or path.is_symlink():
                raise RunnerError("cache_structure_invalid")
            if path.is_file():
                info = path.stat()
                total += max(4096, ((info.st_size + 4095) // 4096) * 4096, info.st_blocks * 512)
            elif path.is_dir():
                total += 4096
        return total

    def reserve(self, category, amount):
        if amount < 0 or self.usage(category) + amount > QUOTAS[category]:
            raise RunnerError("cache_quota_exceeded", status=507)

    def quarantine(self, path, reason):
        with self.lock:
            if path.parent not in [self.root / key for key in QUOTAS if key != "quarantine"]:
                raise RunnerError("quarantine_scope_invalid")
            if not path.exists() and not path.is_symlink():
                return
            entries = [path] + (
                list(path.rglob("*")) if path.is_dir() and not path.is_symlink() else []
            )
            size = sum(
                4096
                if p.is_dir() or p.is_symlink()
                else max(4096, ((p.stat().st_size + 4095) // 4096) * 4096, p.stat().st_blocks * 512)
                for p in entries
            )
            target = self.root / "quarantine" / (path.name + "." + uuid.uuid4().hex)
            if (
                not path.is_symlink()
                and self.usage("quarantine") + size + 4096 <= QUOTAS["quarantine"]
            ):
                if path.is_dir():
                    path.chmod(0o755)
                os.replace(path, target)
            else:
                # Preserve a bounded incident record when retaining all bytes exceeds quota.
                if path.is_dir() and not path.is_symlink():
                    self.remove_tree(path)
                else:
                    path.unlink()
            record = canonical({"name": path.name, "reason": reason, "allocation_bytes": size})
            if self.usage("quarantine") + 4096 <= QUOTAS["quarantine"]:
                target.with_suffix(target.suffix + ".json").write_bytes(record)

    def remove_tree(self, path):
        if path.is_symlink() or not path.is_dir():
            path.unlink()
            return
        for entry in path.rglob("*"):
            if not entry.is_symlink():
                entry.chmod(0o755 if entry.is_dir() else 0o644)
        path.chmod(0o755)
        shutil.rmtree(path)

    def write_wheel(self, expected, data):
        if hashlib.sha256(data).hexdigest() != expected or not re.fullmatch(
            r"[a-f0-9]{64}", expected
        ):
            raise RunnerError("wheel_hash_mismatch")
        with self.lock:
            target = self.root / "wheels" / expected
            if target.exists():
                if (
                    not target.is_symlink()
                    and target.is_file()
                    and hashlib.sha256(target.read_bytes()).hexdigest() == expected
                ):
                    return target
                self.quarantine(target, "wheel_hash_mismatch")
            self.reserve("wheels", allocation_for_files({expected: len(data)}))
            temporary = target.with_name(".tmp-" + uuid.uuid4().hex)
            try:
                with temporary.open("xb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                temporary.chmod(0o444)
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
            return target

    @contextmanager
    def staging(self, category, required):
        with self.lock:
            self.reserve(category, required)
            directory = self.root / category / (".tmp-" + uuid.uuid4().hex)
            directory.mkdir()
            try:
                yield directory
            finally:
                if directory.exists():
                    self.remove_tree(directory)

    def commit_directory(self, temporary, target, manifest):
        signed = canonical(self.manifest(manifest))
        if len(signed) > 512_000:
            raise RunnerError("artifact_manifest_limit")
        with (temporary / MARKER).open("xb") as stream:
            stream.write(signed)
            stream.flush()
            os.fsync(stream.fileno())
        for path in temporary.rglob("*"):
            if path.is_symlink():
                raise RunnerError("artifact_symlink_rejected")
            path.chmod(0o555 if path.is_dir() else 0o444)
        # Rename before sealing root, also supported by the local macOS file tests.
        os.replace(temporary, target)
        target.chmod(0o555)

    def recover_temporary(self):
        for category in ["wheels", "dependencies", "bundles"]:
            for path in (self.root / category).iterdir():
                if re.fullmatch(r"\.tmp-[a-f0-9]{32}", path.name):
                    self.quarantine(path, "interrupted_temporary_write")


def inspect_install_archive(data):
    if len(data) > INSTALL_ARCHIVE_BYTES:
        raise RunnerError("dependency_export_limit")
    entries, total = {}, 0
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
            for member in archive:
                path = PurePosixPath(member.name)
                if (
                    not member.isfile()
                    or not member.name
                    or str(path) != member.name
                    or path.is_absolute()
                    or ".." in path.parts
                    or "\\" in member.name
                    or member.name in entries
                    or member.name == MARKER
                    or len(member.name) > 240
                    or member.size < 0
                ):
                    raise RunnerError("unsafe_dependency_export")
                total += member.size
                if total > INSTALL_BYTES or len(entries) >= INSTALL_FILES:
                    raise RunnerError("dependency_export_limit")
                stream = archive.extractfile(member)
                digest = hashlib.sha256()
                for block in iter(lambda: stream.read(65536), b""):
                    digest.update(block)
                entries[member.name] = {"size": member.size, "sha256": digest.hexdigest()}
    except (tarfile.TarError, OSError, ValueError) as exc:
        raise RunnerError("invalid_dependency_export") from exc
    return entries, total


def extract_install_archive(data, destination, expected):
    # All members were validated before any persistent allocation; never extractall.
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
        for member in archive:
            target = destination / member.name
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as output:
                source = archive.extractfile(member)
                shutil.copyfileobj(source, output, 65536)
            if (
                target.stat().st_size != expected[member.name]["size"]
                or hashlib.sha256(target.read_bytes()).hexdigest()
                != expected[member.name]["sha256"]
            ):
                raise RunnerError("dependency_export_mismatch")
