"""Download metadata and approved pure wheels; never import downloaded packages."""

import email
import hashlib
import io
import json
import os
import re
import time
import urllib.parse
import urllib.request
import zipfile
import uuid
from pathlib import PurePosixPath

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name, parse_wheel_filename
from packaging.version import InvalidVersion, Version
from packaging.specifiers import SpecifierSet

from common import APPROVED, RunnerError


class FixedRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def validate_url(url):
    parsed = urllib.parse.urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"pypi.org", "files.pythonhosted.org"}
        or parsed.port not in {None, 443}
        or parsed.username
        or parsed.password
    ):
        raise RunnerError("dependency_url_rejected")


def pins(values):
    if not isinstance(values, list) or len(values) > 7:
        raise RunnerError("invalid_dependencies")
    found = {}
    for value in values:
        if not isinstance(value, str) or not re.fullmatch(
            r"[A-Za-z0-9_.-]+==[A-Za-z0-9_.+!-]+", value
        ):
            raise RunnerError("exact_dependency_pin_required")
        name, version = value.split("==")
        name = canonicalize_name(name)
        if name not in APPROVED or name in found:
            raise RunnerError("dependency_not_approved_or_duplicate")
        try:
            parsed = Version(version)
        except InvalidVersion as exc:
            raise RunnerError("invalid_version") from exc
        if parsed.is_prerelease or parsed.is_devrelease or parsed.local:
            raise RunnerError("stable_release_required")
        found[name] = str(parsed)
    return [f"{name}=={found[name]}" for name in sorted(found)]


class Resolver:
    def __init__(self, cache_dir):
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), FixedRedirect())
        self.requests = 0
        self.deadline = time.monotonic() + 90
        self.metadata = {}
        self.packages = {}
        self.downloaded_bytes = 0
        self.python_version = "3.12.0"
        self.store = None
        self.wheel_sizes = {}

    def fetch(self, url, limit):
        validate_url(url)
        self.requests += 1
        if self.requests > 48 or time.monotonic() >= self.deadline:
            raise RunnerError("dependency_resolution_budget")
        try:
            with self.opener.open(
                urllib.request.Request(url, headers={"User-Agent": "hr-adapter-runner/1"}),
                timeout=min(15, max(1, self.deadline - time.monotonic())),
            ) as response:
                validate_url(response.url)
                chunks, size = [], 0
                while True:
                    block = response.read(min(65536, limit + 1 - size))
                    if not block:
                        break
                    chunks.append(block)
                    size += len(block)
                    if size > limit or time.monotonic() >= self.deadline:
                        raise RunnerError("dependency_download_budget")
                return b"".join(chunks)
        except RunnerError:
            raise
        except Exception as exc:
            raise RunnerError("dependency_fetch_failed", status=502) from exc

    def meta(self, name, version=None):
        key = (name, version)
        if key not in self.metadata:
            suffix = f"/{version}" if version else ""
            self.metadata[key] = json.loads(
                self.fetch(f"https://pypi.org/pypi/{name}{suffix}/json", 6_000_000)
            )
        return self.metadata[key]

    def versions(self, name, constraints):
        exact = [
            str(spec.version)
            for req in constraints
            for spec in req.specifier
            if spec.operator == "==" and "*" not in spec.version
        ]
        if exact:
            versions = {Version(value) for value in exact}
            return sorted(versions, reverse=True) if len(versions) == 1 else []
        values = []
        for version in self.meta(name).get("releases", {}):
            try:
                parsed = Version(version)
            except InvalidVersion:
                continue
            if not parsed.is_prerelease and not parsed.is_devrelease and not parsed.local:
                values.append(parsed)
        return sorted(values, reverse=True)[:30]

    def wheel(self, name, version):
        key = (name, str(version))
        if key in self.packages:
            return self.packages[key]
        meta = self.meta(name, str(version))
        requires_python = meta.get("info", {}).get("requires_python")
        if requires_python and not SpecifierSet(requires_python).contains(self.python_version):
            return None
        candidates = []
        for item in meta.get("urls", []):
            if item.get("packagetype") != "bdist_wheel" or item.get("yanked"):
                continue
            try:
                wn, wv, _, tags = parse_wheel_filename(item["filename"])
            except Exception:
                continue
            if (
                wn != name
                or wv != version
                or not tags
                or not all(t.abi == "none" and t.platform == "any" for t in tags)
                or not any(t.interpreter == "py3" for t in tags)
            ):
                continue
            if item.get("requires_python") and not SpecifierSet(item["requires_python"]).contains(
                self.python_version
            ):
                continue
            candidates.append(item)
        if not candidates:
            return None
        item = sorted(candidates, key=lambda x: x["filename"])[0]
        filename, expected = item["filename"], item["digests"]["sha256"]
        if not re.fullmatch(r"[A-Za-z0-9_.+-]+\.whl", filename) or not re.fullmatch(
            r"[a-f0-9]{64}", expected
        ):
            raise RunnerError("invalid_wheel_metadata")
        location = self.cache_dir / expected
        if location.is_file() and not location.is_symlink():
            data = location.read_bytes()
            if hashlib.sha256(data).hexdigest() != expected:
                if self.store is None:
                    raise RunnerError("wheel_hash_mismatch")
                self.store.quarantine(location, "wheel_hash_mismatch")
                data = None
        else:
            data = None
            if location.exists() or location.is_symlink():
                if self.store is None:
                    raise RunnerError("wheel_hash_mismatch")
                self.store.quarantine(location, "invalid_wheel_cache")
        if data is None:
            data = self.fetch(item["url"], 8_000_000)
            self.downloaded_bytes += len(data)
            if self.downloaded_bytes > 25_000_000:
                raise RunnerError("dependency_download_budget")
        if hashlib.sha256(data).hexdigest() != expected:
            raise RunnerError("wheel_hash_mismatch")
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                infos = archive.infolist()
                if len(infos) > 2048 or sum(f.file_size for f in infos) > 32_000_000:
                    raise RunnerError("wheel_expansion_budget")
                for entry in infos:
                    path = PurePosixPath(entry.filename)
                    mode = entry.external_attr >> 16
                    if (
                        path.is_absolute()
                        or ".." in path.parts
                        or "\\" in entry.filename
                        or (mode & 0o170000) not in {0, 0o040000, 0o100000}
                        or entry.filename.endswith(".pth")
                    ):
                        raise RunnerError("unsafe_wheel_member")
                metadata_files = [
                    f.filename for f in infos if f.filename.endswith(".dist-info/METADATA")
                ]
                if len(metadata_files) != 1:
                    raise RunnerError("invalid_wheel_metadata")
                msg = email.message_from_bytes(archive.read(metadata_files[0]))
                self.wheel_sizes[expected] = (sum(f.file_size for f in infos), len(infos))
        except (zipfile.BadZipFile, KeyError) as exc:
            raise RunnerError("invalid_wheel_archive") from exc
        if (
            canonicalize_name(msg["Name"] or "") != name
            or Version(msg["Version"] or "0") != version
        ):
            raise RunnerError("wheel_identity_mismatch")
        deps = []
        environment = {
            **default_environment(),
            "python_version": "3.12",
            "python_full_version": self.python_version,
            "sys_platform": "linux",
            "os_name": "posix",
            "platform_system": "Linux",
            "platform_machine": "x86_64",
            "extra": "",
        }
        for raw in msg.get_all("Requires-Dist", []):
            req = Requirement(raw)
            if req.marker and not req.marker.evaluate(environment):
                continue
            if canonicalize_name(req.name) not in APPROVED or req.url or req.extras:
                raise RunnerError("transitive_dependency_not_approved")
            deps.append(req)
        if not location.exists():
            if self.store is not None:
                self.store.write_wheel(expected, data)
            else:
                temporary = location.with_name(".tmp-" + uuid.uuid4().hex)
                try:
                    with temporary.open("xb") as output:
                        output.write(data)
                        output.flush()
                        os.fsync(output.fileno())
                    temporary.chmod(0o444)
                    os.replace(temporary, location)
                finally:
                    temporary.unlink(missing_ok=True)
        result = (
            {"name": name, "version": str(version), "filename": filename, "sha256": expected},
            deps,
        )
        self.packages[key] = result
        return result

    def resolve(self, dependencies):
        requested = pins(dependencies)

        def solve(requirements, selected):
            groups = {}
            for req in requirements:
                groups.setdefault(canonicalize_name(req.name), []).append(req)
            for name, (wheel, _) in selected.items():
                if not all(
                    req.specifier.contains(wheel["version"], prereleases=False)
                    for req in groups[name]
                ):
                    return None
            missing = sorted(set(groups) - set(selected))
            if not missing:
                return selected
            name = missing[0]
            for version in self.versions(name, groups[name]):
                if not all(
                    req.specifier.contains(version, prereleases=False) for req in groups[name]
                ):
                    continue
                found = self.wheel(name, version)
                if found:
                    result = solve(requirements + found[1], {**selected, name: found})
                    if result is not None:
                        return result
            return None

        selected = solve([Requirement(value) for value in requested], {})
        if selected is None:
            raise RunnerError("dependency_resolution_failed")
        sizes = [self.wheel_sizes[value[0]["sha256"]] for value in selected.values()]
        if sum(value[0] for value in sizes) > 48_000_000 or sum(value[1] for value in sizes) > 3000:
            raise RunnerError("combined_wheel_expansion_budget")
        return {
            "schema_version": 1,
            "python": "3.12",
            "platform": "linux/amd64",
            "dependencies": requested,
            "wheels": [selected[name][0] for name in sorted(selected)],
            "resolver_policy": "approved-pure-wheels-v1",
        }
