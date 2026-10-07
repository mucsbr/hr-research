"""Small trusted broker with durable idempotency, no application credentials."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import fcntl
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import threading
import time
import urllib.parse
import uuid

from common import (
    LIMITS,
    RESPONSE_MAX_BYTES,
    STATE_MAX_BYTES,
    RunnerError,
    canonical,
    digest,
    encode_state,
    exact_fields,
    parse_json,
    parse_state,
    sha,
    sign_receipt,
    validate_bundle,
    validate_run,
)
from docker_backend import DockerBackend
from resolver import Resolver, pins
from storage import (
    ArtifactStore,
    MARKER,
    inspect_install_archive,
    extract_install_archive,
    allocation_for_files,
)

TERMINAL = {"succeeded", "failed", "cancelled", "unknown"}


def atomic_write(path, data, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with temporary.open("xb") as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())
    temporary.chmod(mode)
    os.replace(temporary, path)


def directory_digest(directory, seal=False):
    files, size = {}, 0
    paths = sorted(directory.rglob("*"))
    if len(paths) > 4096:
        raise RunnerError("dependency_file_limit")
    for path in paths:
        if path == directory / MARKER:
            continue
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not (
            stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)
        ):
            raise RunnerError("unsafe_dependency_environment")
        if path.is_file():
            size += info.st_size
            if size > 64_000_000 or info.st_nlink > 1:
                raise RunnerError("dependency_environment_limit")
            files[str(path.relative_to(directory))] = hashlib.sha256(path.read_bytes()).hexdigest()
            if seal:
                path.chmod(0o444)
        elif seal:
            path.chmod(0o555)
    if seal:
        directory.chmod(0o555)
    return digest(files)


class Broker:
    def __init__(self, root, token, backend, resolver_factory=Resolver):
        if len(token) < 32:
            raise RunnerError("runner_token_too_short")
        self.root, self.token, self.backend, self.resolver_factory = (
            root,
            token,
            backend,
            resolver_factory,
        )
        self.root.mkdir(parents=True, exist_ok=True)
        for name in ["bundles", "wheels", "dependencies", "builds", "runs"]:
            (root / name).mkdir(exist_ok=True)
        self.lock = threading.RLock()
        self.resolve_lock = threading.Lock()
        self.artifacts = ArtifactStore(root, token)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="isolated-parser")
        self.db_path = root / "state.sqlite3"
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS locks (sha TEXT PRIMARY KEY, data BLOB NOT NULL, environment_sha TEXT NOT NULL, evidence BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS bundles (sha TEXT PRIMARY KEY, lock_sha TEXT NOT NULL, data BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, idem TEXT UNIQUE NOT NULL, request_sha TEXT NOT NULL,
                    request BLOB NOT NULL, state BLOB NOT NULL, cancel INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS quarantined_states (run_id TEXT NOT NULL, sample_sha TEXT NOT NULL,
                    state_sample BLOB NOT NULL, reason TEXT NOT NULL, created_at REAL NOT NULL,
                    PRIMARY KEY (run_id, sample_sha));
            """)
        self.db_path.chmod(0o600)

    @contextmanager
    def db(self):
        connection = sqlite3.connect(self.db_path, timeout=20)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def recover(self):
        self.backend.verify_runtime()
        self.backend.cleanup_owned()
        for path in (self.root / "builds").iterdir():
            if re.fullmatch(r"[a-f0-9]{32}", path.name):
                self.artifacts.remove_tree(path)
        self.artifacts.recover_temporary()
        self.recover_artifacts()
        with self.lock, self.db() as db:
            rows = db.execute(
                "SELECT id,idem,request_sha,substr(request,1,3000001) AS request, "
                "substr(state,1,?) AS state FROM runs",
                (STATE_MAX_BYTES + 1,),
            )
            for row in rows:
                try:
                    state = parse_state(row["state"])
                    if (
                        state["run_id"] != row["id"]
                        or state["request_sha256"] != row["request_sha"]
                    ):
                        raise RunnerError("stored_identity_mismatch")
                    if state["status"] in TERMINAL:
                        receipt = state["receipt"]
                        if (
                            not isinstance(receipt, dict)
                            or receipt.get("signature")
                            != sign_receipt(receipt, self.token)["signature"]
                        ):
                            raise RunnerError("stored_receipt_invalid")
                        if state["output"] is None and receipt.get("output_sha256") is None:
                            receipt["output_sha256"] = digest(None)
                            receipt.setdefault(
                                "execution_started",
                                bool(receipt.get("container_id")) or state["status"] == "unknown",
                            )
                            receipt["null_output_binding_upgraded"] = True
                            state["receipt"] = sign_receipt(receipt, self.token)
                            db.execute(
                                "UPDATE runs SET state=? WHERE id=?",
                                (encode_state(state), row["id"]),
                            )
                        continue
                    state = self.recovered_state(
                        row, "broker_restarted", state["status"] == "running"
                    )
                except (RunnerError, TypeError, ValueError, KeyError, AttributeError):
                    sample = (
                        row["state"]
                        if isinstance(row["state"], bytes)
                        else str(row["state"]).encode()
                    )
                    db.execute(
                        "INSERT OR IGNORE INTO quarantined_states VALUES (?,?,?,?,?)",
                        (
                            row["id"],
                            hashlib.sha256(sample).hexdigest(),
                            sample,
                            "stored_state_corrupt",
                            time.time(),
                        ),
                    )
                    state = self.recovered_state(row, "stored_state_corrupt")
                db.execute(
                    "UPDATE runs SET state=?,cancel=1 WHERE id=?", (encode_state(state), row["id"])
                )

    def recovered_state(self, row, reason, may_have_executed=True):
        receipt = None
        try:
            request = parse_json(row["request"], 3_000_000)
            validate_run(request)
            if digest(request) != row["request_sha"]:
                raise RunnerError("stored_request_corrupt")
            receipt = self.base_receipt(row["id"], request)
            receipt.update(
                status="unknown",
                error_code=reason,
                output_sha256=digest(None),
                execution_started=may_have_executed,
                cleanup={"container_removed": True, "process_group_terminated": True},
                finished_at=time.time(),
            )
            receipt = sign_receipt(receipt, self.token)
        except (RunnerError, TypeError, ValueError, KeyError, AttributeError):
            # Preserve identity and refusal even when a corrupted request cannot be attested.
            pass
        return {
            "run_id": row["id"],
            "status": "unknown",
            "request_sha256": row["request_sha"],
            "output": None,
            "error_code": reason,
            "receipt": receipt,
        }

    def resolve(self, body):
        exact_fields(body, {"dependencies"})
        requested = pins(body["dependencies"])
        # A bounded single resolver prevents duplicate downloads and disk races.
        if not self.resolve_lock.acquire(blocking=False):
            raise RunnerError("resolver_busy", status=429)
        try:
            resolver = self.resolver_factory(self.root / "wheels")
            resolver.python_version = getattr(self.backend, "python_version", None) or "3.12.0"
            resolver.store = self.artifacts
            lock = resolver.resolve(requested)
            lock_sha = digest(lock)
            with self.db() as db:
                existing = db.execute("SELECT * FROM locks WHERE sha=?", (lock_sha,)).fetchone()
            if existing:
                try:
                    self.verify_environment(existing)
                    return {"lock": lock, "lock_sha256": lock_sha}
                except RunnerError:
                    pass  # Explicit resolve may rebuild a missing/old/tampered environment.
            if self.adopt_dependency(lock_sha, require_current_runtime=True):
                return {"lock": lock, "lock_sha256": lock_sha}
            with self.db() as db:
                if not existing and db.execute("SELECT COUNT(*) FROM locks").fetchone()[0] >= 64:
                    raise RunnerError("lock_cache_limit", status=507)
            build = self.root / "builds" / uuid.uuid4().hex
            wheels = build / "wheels"
            try:
                self.prepare_wheelhouse(wheels, lock)
                evidence = self.backend.execute(mode="install", mounts=[(wheels, "/wheels", True)])
                if evidence["error_code"] or evidence["exit_code"] != 0 or not evidence["cleanup"]:
                    raise RunnerError("dependency_install_failed", status=422)
                entries, total = inspect_install_archive(evidence["stdout"])
                environment_sha = digest({name: entry["sha256"] for name, entry in entries.items()})
                destination = self.root / "dependencies" / lock_sha
                safe_evidence = {
                    key: value for key, value in evidence.items() if key not in {"stdout", "stderr"}
                }
                manifest = {
                    "artifact_type": "dependency",
                    "lock": lock,
                    "environment_sha": environment_sha,
                    "evidence": safe_evidence,
                }
                allocation = allocation_for_files(
                    {
                        **{name: entry["size"] for name, entry in entries.items()},
                        MARKER: len(canonical(self.artifacts.manifest(manifest))),
                    }
                )
                with self.artifacts.staging("dependencies", allocation) as temporary:
                    extract_install_archive(evidence["stdout"], temporary, entries)
                    if directory_digest(temporary) != environment_sha:
                        raise RunnerError("dependency_export_mismatch")
                    if destination.exists():
                        self.artifacts.quarantine(
                            destination, "dependency_replaced_after_validation"
                        )
                    self.artifacts.commit_directory(temporary, destination, manifest)
                with self.db() as db:
                    db.execute(
                        "INSERT OR REPLACE INTO locks VALUES (?,?,?,?)",
                        (lock_sha, canonical(lock), environment_sha, canonical(safe_evidence)),
                    )
            finally:
                shutil.rmtree(build, ignore_errors=True)
            return {"lock": lock, "lock_sha256": lock_sha}
        finally:
            self.resolve_lock.release()

    def prepare_wheelhouse(self, directory, lock):
        directory.mkdir(parents=True)
        requirements = []
        for wheel in lock["wheels"]:
            source = self.root / "wheels" / wheel["sha256"]
            if hashlib.sha256(source.read_bytes()).hexdigest() != wheel["sha256"]:
                raise RunnerError("wheel_hash_mismatch")
            os.link(source, directory / wheel["filename"])
            requirements.append(
                f"{wheel['name']}=={wheel['version']} --hash=sha256:{wheel['sha256']}"
            )
        atomic_write(
            directory / "requirements.lock", ("\n".join(requirements) + "\n").encode(), 0o444
        )

    def adopt_dependency(self, lock_sha, require_current_runtime=False):
        directory = self.root / "dependencies" / lock_sha
        if not directory.exists():
            return False
        try:
            manifest = self.artifacts.read_manifest(directory)
            lock = manifest["lock"]
            if (
                manifest.get("artifact_type") != "dependency"
                or digest(lock) != lock_sha
                or directory_digest(directory) != manifest["environment_sha"]
            ):
                raise RunnerError("orphan_dependency_invalid")
            for wheel in lock["wheels"]:
                file = self.root / "wheels" / wheel["sha256"]
                if (
                    not file.is_file()
                    or file.is_symlink()
                    or hashlib.sha256(file.read_bytes()).hexdigest() != wheel["sha256"]
                ):
                    raise RunnerError("orphan_wheel_invalid")
            if (
                require_current_runtime
                and manifest["evidence"]["runtime_sha256"] != self.backend.image
            ):
                return False
            with self.db() as db:
                db.execute(
                    "INSERT OR REPLACE INTO locks VALUES (?,?,?,?)",
                    (
                        lock_sha,
                        canonical(lock),
                        manifest["environment_sha"],
                        canonical(manifest["evidence"]),
                    ),
                )
            return True
        except (RunnerError, ValueError, KeyError, OSError, TypeError):
            self.artifacts.quarantine(directory, "invalid_orphan_dependency")
            return False

    def check_bundle_files(self, directory, bundle):
        paths = list(directory.rglob("*"))
        actual = sorted(
            str(p.relative_to(directory)) for p in paths if p.is_file() and p != directory / MARKER
        )
        if any(p.is_symlink() for p in paths) or actual != sorted(bundle["files"]):
            raise RunnerError("stored_bundle_tampered")
        for name, source in bundle["files"].items():
            if (directory / name).read_bytes() != source.encode():
                raise RunnerError("stored_bundle_tampered")

    def recover_artifacts(self):
        # Only authenticated manifests in this store are eligible for DB reconciliation.
        for directory in (self.root / "dependencies").iterdir():
            if re.fullmatch(r"[a-f0-9]{64}", directory.name):
                try:
                    with self.db() as db:
                        row = db.execute(
                            "SELECT * FROM locks WHERE sha=?", (directory.name,)
                        ).fetchone()
                    if row and not (directory / MARKER).exists():
                        # Preserve pre-manifest deployments only after verifying their DB hash.
                        if directory_digest(directory) == row["environment_sha"]:
                            manifest = {
                                "artifact_type": "dependency",
                                "lock": parse_json(row["data"]),
                                "environment_sha": row["environment_sha"],
                                "evidence": parse_json(row["evidence"]),
                            }
                            encoded = canonical(self.artifacts.manifest(manifest))
                            self.artifacts.reserve(
                                "dependencies", allocation_for_files({MARKER: len(encoded)})
                            )
                            directory.chmod(0o755)
                            atomic_write(directory / MARKER, encoded, 0o444)
                            directory.chmod(0o555)
                    self.adopt_dependency(directory.name)
                except (RunnerError, KeyError, ValueError, TypeError, OSError):
                    self.artifacts.quarantine(directory, "invalid_dependency_artifact")
        for directory in (self.root / "bundles").iterdir():
            if not re.fullmatch(r"[a-f0-9]{64}", directory.name):
                continue
            try:
                with self.db() as db:
                    row = db.execute(
                        "SELECT * FROM bundles WHERE sha=?", (directory.name,)
                    ).fetchone()
                if row and not (directory / MARKER).exists():
                    bundle = parse_json(row["data"])
                    self.check_bundle_files(directory, bundle)
                    encoded = canonical(
                        self.artifacts.manifest({"artifact_type": "bundle", "bundle": bundle})
                    )
                    self.artifacts.reserve("bundles", allocation_for_files({MARKER: len(encoded)}))
                    directory.chmod(0o755)
                    atomic_write(directory / MARKER, encoded, 0o444)
                    directory.chmod(0o555)
                marker = self.artifacts.read_manifest(directory)
                bundle = marker["bundle"]
                validate_bundle(bundle)
                if marker.get("artifact_type") != "bundle" or digest(bundle) != directory.name:
                    raise RunnerError("orphan_bundle_invalid")
                self.check_bundle_files(directory, bundle)
                with self.db() as db:
                    if db.execute(
                        "SELECT 1 FROM locks WHERE sha=?", (digest(bundle["lock"]),)
                    ).fetchone():
                        db.execute(
                            "INSERT OR IGNORE INTO bundles VALUES (?,?,?)",
                            (directory.name, digest(bundle["lock"]), canonical(bundle)),
                        )
            except (RunnerError, KeyError, ValueError, TypeError, OSError):
                self.artifacts.quarantine(directory, "invalid_orphan_bundle")

    def verify_environment(self, lock):
        evidence = parse_json(lock["evidence"])
        if evidence.get("runtime_sha256") != self.backend.image:
            raise RunnerError("dependency_runtime_changed", status=409)
        directory = self.root / "dependencies" / lock["sha"]
        if not directory.is_dir() or directory_digest(directory) != lock["environment_sha"]:
            raise RunnerError("dependency_environment_tampered", status=409)
        lock_data = parse_json(lock["data"])
        if digest(lock_data) != lock["sha"]:
            raise RunnerError("stored_lock_tampered", status=409)
        for wheel in lock_data["wheels"]:
            source = self.root / "wheels" / wheel["sha256"]
            if (
                not source.is_file()
                or hashlib.sha256(source.read_bytes()).hexdigest() != wheel["sha256"]
            ):
                raise RunnerError("wheel_hash_mismatch", status=409)

    def put_bundle(self, expected, bundle):
        sha(expected)
        validate_bundle(bundle)
        if digest(bundle) != expected:
            raise RunnerError("bundle_hash_mismatch")
        lock_sha = digest(bundle["lock"])
        with self.lock, self.db() as db:
            lock = db.execute("SELECT * FROM locks WHERE sha=?", (lock_sha,)).fetchone()
            if (
                not lock
                or canonical(bundle["lock"]) != lock["data"]
                or pins(bundle["dependencies"]) != bundle["lock"]["dependencies"]
            ):
                raise RunnerError("untrusted_dependency_lock")
            self.verify_environment(lock)
            existing = db.execute("SELECT data FROM bundles WHERE sha=?", (expected,)).fetchone()
            if existing:
                if existing["data"] != canonical(bundle):
                    raise RunnerError("bundle_conflict", status=409)
                try:
                    self.check_bundle_files(self.root / "bundles" / expected, bundle)
                    return {"bundle_sha256": expected, "lock_sha256": lock_sha, "created": False}
                except (RunnerError, OSError):
                    self.artifacts.quarantine(
                        self.root / "bundles" / expected, "damaged_registered_bundle"
                    )
            target = self.root / "bundles" / expected
            if target.exists():
                try:
                    marker = self.artifacts.read_manifest(target)
                    if marker.get("artifact_type") != "bundle" or marker.get("bundle") != bundle:
                        raise RunnerError("orphan_bundle_invalid")
                    self.check_bundle_files(target, bundle)
                except (RunnerError, OSError):
                    self.artifacts.quarantine(target, "invalid_orphan_bundle")
            if not target.exists():
                if db.execute("SELECT COUNT(*) FROM bundles").fetchone()[0] >= 256:
                    raise RunnerError("bundle_cache_limit", status=507)
                manifest = {"artifact_type": "bundle", "bundle": bundle}
                size = allocation_for_files(
                    {
                        **{name: len(source.encode()) for name, source in bundle["files"].items()},
                        MARKER: len(canonical(self.artifacts.manifest(manifest))),
                    }
                )
                with self.artifacts.staging("bundles", size) as temporary:
                    for name, content in bundle["files"].items():
                        atomic_write(temporary / name, content.encode(), 0o444)
                    self.artifacts.commit_directory(temporary, target, manifest)
            db.execute(
                "INSERT OR REPLACE INTO bundles VALUES (?,?,?)",
                (expected, lock_sha, canonical(bundle)),
            )
        return {"bundle_sha256": expected, "lock_sha256": lock_sha, "created": True}

    def base_receipt(self, run_id, request):
        with self.db() as db:
            bundle = db.execute(
                "SELECT * FROM bundles WHERE sha=?", (request["bundle_sha256"],)
            ).fetchone()
            lock = (
                db.execute("SELECT * FROM locks WHERE sha=?", (bundle["lock_sha"],)).fetchone()
                if bundle
                else None
            )
        return {
            "schema_version": 1,
            "run_id": run_id,
            "operation": request["operation"],
            "request_sha256": digest(request),
            "input_sha256": digest(request["input"]),
            "bundle_sha256": request["bundle_sha256"],
            "lock_sha256": bundle["lock_sha"] if bundle else None,
            "runtime_sha256": self.backend.image,
            "limits": dict(LIMITS),
            "dependency_environment_sha256": lock["environment_sha"] if lock else None,
            "dependency_installation_sha256": hashlib.sha256(lock["evidence"]).hexdigest()
            if lock
            else None,
            "exit_code": None,
            "oom_killed": False,
            "cleanup": {"container_removed": True, "process_group_terminated": True},
            "execution_started": False,
            "output_sha256": digest(None),
        }

    def submit(self, request):
        validate_run(request)
        request_sha = digest(request)
        with self.lock, self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT * FROM runs WHERE idem=?", (request["idempotency_key"],)
            ).fetchone()
            if existing:
                if existing["request_sha"] != request_sha:
                    raise RunnerError("idempotency_conflict", status=409)
                return parse_state(existing["state"]), False
            if not db.execute(
                "SELECT 1 FROM bundles WHERE sha=?", (request["bundle_sha256"],)
            ).fetchone():
                raise RunnerError("bundle_not_found", status=404)
            if db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] >= 1024:
                raise RunnerError("run_storage_limit", status=429)
            run_id = str(uuid.uuid4())
            state = {
                "run_id": run_id,
                "status": "queued",
                "request_sha256": request_sha,
                "output": None,
                "error_code": None,
                "receipt": None,
            }
            db.execute(
                "INSERT INTO runs(id,idem,request_sha,request,state) VALUES (?,?,?,?,?)",
                (
                    run_id,
                    request["idempotency_key"],
                    request_sha,
                    canonical(request),
                    encode_state(state),
                ),
            )
        self.executor.submit(self.execute_run, run_id)
        return state, True

    def get(self, identifier, by_key=False):
        with self.db() as db:
            row = db.execute(
                "SELECT state FROM runs WHERE " + ("idem" if by_key else "id") + "=?", (identifier,)
            ).fetchone()
        if not row:
            raise RunnerError("run_not_found", status=404)
        return parse_state(row["state"])

    def cancel(self, run_id):
        with self.lock, self.db() as db:
            row = db.execute("SELECT state FROM runs WHERE id=?", (run_id,)).fetchone()
            if not row:
                raise RunnerError("run_not_found", status=404)
            state = parse_state(row["state"])
            if state["status"] not in TERMINAL:
                db.execute("UPDATE runs SET cancel=1 WHERE id=?", (run_id,))
        return self.get(run_id)

    def is_cancelled(self, run_id):
        with self.db() as db:
            row = db.execute("SELECT cancel FROM runs WHERE id=?", (run_id,)).fetchone()
        return not row or bool(row["cancel"])

    def execute_run(self, run_id):
        receipt, output, error, status = None, None, None, "unknown"
        try:
            with self.lock, self.db() as db:
                row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
                state = parse_state(row["state"])
                if state["status"] != "queued":
                    return
                request = parse_json(row["request"])
                state["status"] = "running"
                db.execute("UPDATE runs SET state=? WHERE id=?", (encode_state(state), run_id))
            receipt = self.base_receipt(run_id, request)
            receipt["started_at"] = time.time()
            if self.is_cancelled(run_id):
                status, error = "cancelled", "cancelled"
                receipt["cleanup"] = {"container_removed": True, "process_group_terminated": True}
                receipt["execution_started"] = False
            else:
                with self.db() as db:
                    bundle_row = db.execute(
                        "SELECT * FROM bundles WHERE sha=?", (request["bundle_sha256"],)
                    ).fetchone()
                    lock = db.execute(
                        "SELECT * FROM locks WHERE sha=?", (bundle_row["lock_sha"],)
                    ).fetchone()
                self.verify_environment(lock)
                bundle = parse_json(bundle_row["data"])
                bundle_path = self.root / "bundles" / bundle_row["sha"]
                if digest(bundle) != bundle_row["sha"]:
                    raise RunnerError("stored_bundle_tampered")
                self.check_bundle_files(bundle_path, bundle)
                input_path = self.root / "runs" / run_id / "input"
                atomic_write(
                    input_path / "request.json",
                    canonical({"operation": request["operation"], "input": request["input"]}),
                    0o444,
                )
                receipt["execution_started"] = True
                receipt["cleanup"] = {"container_removed": False, "process_group_terminated": False}
                result = self.backend.execute(
                    mode="run",
                    mounts=[
                        (bundle_path, "/bundle", True),
                        (self.root / "dependencies" / lock["sha"], "/deps", True),
                        (input_path, "/input", True),
                    ],
                    cancelled=lambda: self.is_cancelled(run_id),
                )
                receipt.update(
                    {key: value for key, value in result.items() if key not in {"stdout", "stderr"}}
                )
                receipt["cleanup"] = {
                    "container_removed": result["cleanup"],
                    "process_group_terminated": result["cleanup"],
                }
                receipt["execution_started"] = bool(result["container_id"])
                error = result["error_code"]
                if self.is_cancelled(run_id) or error == "cancelled":
                    status, error = "cancelled", "cancelled"
                elif error or result["exit_code"] != 0:
                    status, error = (
                        "failed",
                        error
                        or ("oom_killed" if result["oom_killed"] else "candidate_exit_nonzero"),
                    )
                else:
                    output = parse_json(result["stdout"], LIMITS["stdout_bytes"])
                    if not isinstance(output, dict):
                        raise RunnerError("output_object_required")
                    receipt["output_sha256"] = digest(output)
                    status = "succeeded"
                receipt["stderr_excerpt"] = result["stderr"][:4096].decode(
                    "utf-8", errors="replace"
                )
        except RunnerError as exc:
            status, error = "failed", exc.code
        except Exception:
            status, error = "unknown", "broker_execution_interrupted"
        finally:
            if receipt is not None:
                receipt.update(status=status, error_code=error, finished_at=time.time())
                final_output = output if status == "succeeded" else None
                receipt["output_sha256"] = digest(final_output)
                state.update(
                    status=status,
                    error_code=error,
                    output=final_output,
                    receipt=sign_receipt(receipt, self.token),
                )
                try:
                    encoded = encode_state(state)
                except RunnerError:
                    receipt.update(
                        status="failed",
                        error_code="result_envelope_limit",
                        output_sha256=digest(None),
                    )
                    state.update(
                        status="failed",
                        error_code="result_envelope_limit",
                        output=None,
                        receipt=sign_receipt(receipt, self.token),
                    )
                    encoded = encode_state(state)
                with self.lock, self.db() as db:
                    db.execute("UPDATE runs SET state=? WHERE id=?", (encoded, run_id))


class Handler(BaseHTTPRequestHandler):
    server_version = "AdapterRunner/1"
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, *_):
        pass  # Neither request bodies nor authentication appear in access logs.

    def handle_request(self):
        try:
            authorization = self.headers.get("Authorization", "")
            expected = "Bearer " + self.server.broker.token
            if not hmac.compare_digest(authorization.encode(), expected.encode()):
                raise RunnerError("unauthorized", status=401)
            path = urllib.parse.urlsplit(self.path)
            if path.query or path.fragment:
                raise RunnerError("query_not_supported")
            route = urllib.parse.unquote(path.path)
            body = None
            if self.command in {"POST", "PUT"}:
                if (
                    self.headers.get("Transfer-Encoding")
                    or not self.headers.get("Content-Length", "").isdigit()
                ):
                    raise RunnerError("content_length_required", status=411)
                size = int(self.headers["Content-Length"])
                if size > 3_000_000:
                    raise RunnerError("body_too_large", status=413)
                raw = self.rfile.read(size)
                if len(raw) != size:
                    raise RunnerError("incomplete_request")
                body = parse_json(raw, 3_000_000)
            broker, code = self.server.broker, 200
            if self.command == "GET" and route == "/v1/health":
                result = {
                    "status": "ready",
                    "contract": "python_adapter_v1",
                    "runtime_sha256": broker.backend.image,
                }
            elif self.command == "POST" and route == "/v1/dependencies/resolve":
                result = broker.resolve(body)
            elif self.command == "PUT" and route.startswith("/v1/bundles/"):
                result = broker.put_bundle(route.removeprefix("/v1/bundles/"), body)
                code = 201 if result["created"] else 200
            elif self.command == "POST" and route == "/v1/runs":
                result, _ = broker.submit(body)
                code = 202
            elif self.command == "GET" and route.startswith("/v1/runs/by-key/"):
                key = route.removeprefix("/v1/runs/by-key/")
                if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", key):
                    raise RunnerError("invalid_idempotency_key")
                result = broker.get(key, by_key=True)
            elif self.command in {"GET", "DELETE"} and route.startswith("/v1/runs/"):
                run_id = route.removeprefix("/v1/runs/")
                try:
                    uuid.UUID(run_id)
                except ValueError as exc:
                    raise RunnerError("invalid_run_id") from exc
                result = broker.get(run_id) if self.command == "GET" else broker.cancel(run_id)
            else:
                raise RunnerError("not_found", status=404)
        except RunnerError as exc:
            code, result = exc.status, {"error_code": exc.code, "message": exc.message}
        except Exception:
            code, result = 500, {"error_code": "internal_error"}
        encoded = canonical(result)
        if len(encoded) > RESPONSE_MAX_BYTES:
            code = 500
            encoded = canonical({"error_code": "response_envelope_limit"})
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(encoded)
        self.close_connection = True

    do_GET = do_POST = do_PUT = do_DELETE = handle_request


def main():
    root = Path(os.environ.get("RUNNER_DATA_DIR", "/data"))
    root.mkdir(parents=True, exist_ok=True)
    singleton = (root / "broker.lock").open("a")
    fcntl.flock(singleton, fcntl.LOCK_EX | fcntl.LOCK_NB)
    token = os.environ.get("RUNNER_TOKEN", "")
    backend = DockerBackend(
        root,
        Path(os.environ["RUNNER_HOST_DATA_DIR"]),
        os.environ["RUNNER_RUNTIME_IMAGE"],
        os.environ.get("RUNNER_SCOPE", "hr-adapter-runner-v1"),
    )
    broker = Broker(root, token, backend)
    broker.recover()
    server = ThreadingHTTPServer(("0.0.0.0", 8090), Handler)
    server.broker = broker
    server.serve_forever(poll_interval=0.25)


if __name__ == "__main__":
    main()
