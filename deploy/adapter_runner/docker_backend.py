"""Trusted Docker controller. No request contributes flags, images, or commands."""

import hashlib
import json
import os
import re
import selectors
import signal
import subprocess
import threading
import time
import uuid

from common import LIMITS, RunnerError
from storage import INSTALL_ARCHIVE_BYTES

OWNER = "org.hr-research.adapter-runner"


class DockerBackend:
    def __init__(self, data_root, host_root, image, scope="hr-adapter-runner-v1"):
        if (
            not image.startswith("sha256:")
            or len(image) != 71
            or any(c not in "0123456789abcdef" for c in image[7:])
        ):
            raise RunnerError("fixed_runtime_image_id_required")
        if not host_root.is_absolute() or "," in str(host_root):
            raise RunnerError("invalid_host_data_root")
        self.root, self.host_root, self.image, self.scope = (
            data_root.resolve(),
            host_root,
            image,
            scope,
        )
        self.execution_lock = threading.Lock()
        self.python_version = None

    def command(self, arguments, timeout=20, check=True):
        try:
            result = subprocess.run(
                ["docker", *arguments],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RunnerError("docker_unavailable", status=503) from exc
        if check and result.returncode:
            raise RunnerError("docker_operation_failed", status=503)
        return result

    def verify_runtime(self):
        result = self.command(
            ["image", "inspect", "--format", "{{.Id}} {{.Os}} {{.Architecture}}", self.image]
        )
        if result.stdout.decode().strip() != f"{self.image} linux amd64":
            raise RunnerError("runtime_image_mismatch", status=503)
        environment = json.loads(
            self.command(
                ["image", "inspect", "--format", "{{json .Config.Env}}", self.image]
            ).stdout
        )
        versions = [
            value.split("=", 1)[1] for value in environment if value.startswith("PYTHON_VERSION=")
        ]
        if len(versions) != 1 or not re.fullmatch(r"3\.12\.\d+", versions[0]):
            raise RunnerError("runtime_python_mismatch", status=503)
        self.python_version = versions[0]
        info = json.loads(self.command(["info", "--format", "{{json .SecurityOptions}}"]).stdout)
        if not any("seccomp" in value for value in info) or not any(
            "apparmor" in value for value in info
        ):
            raise RunnerError("required_kernel_isolation_unavailable", status=503)
        return self.image

    def mount(self, path, destination, readonly=True):
        relative = path.resolve().relative_to(self.root)
        origin = self.host_root / relative
        if "," in str(origin):
            raise RunnerError("invalid_mount")
        return [
            "--mount",
            f"type=bind,src={origin},dst={destination}" + (",readonly" if readonly else ""),
        ]

    def execute(self, *, mode, mounts, cancelled=lambda: False):
        with self.execution_lock:
            return self._execute(mode=mode, mounts=mounts, cancelled=cancelled)

    def _execute(self, *, mode, mounts, cancelled=lambda: False):
        if mode not in {"install", "run"}:
            raise RunnerError("invalid_execution_mode")
        timeout = 90 if mode == "install" else LIMITS["timeout_seconds"]
        memory = 536_870_912 if mode == "install" else LIMITS["memory_bytes"]
        name = "hr-adapter-" + uuid.uuid4().hex
        args = [
            "create",
            "--name",
            name,
            "--label",
            f"{OWNER}={self.scope}",
            "--label",
            f"{OWNER}.expires={int(time.time() + timeout + 30)}",
            "--pull",
            "never",
            "--network",
            "none",
            "--read-only",
            "--user",
            "65532:65532",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--security-opt",
            "apparmor=docker-default",
            "--cgroupns",
            "private",
            "--ipc",
            "private",
            "--pids-limit",
            "32",
            "--cpus",
            "1",
            "--memory",
            str(memory),
            "--memory-swap",
            str(memory),
            "--ulimit",
            "nofile=128:128",
            "--ulimit",
            "core=0:0",
            "--tmpfs",
            "/work:rw,nosuid,nodev,size=96m,uid=65532,gid=65532,mode=0700",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=16m,mode=1777",
            "--shm-size",
            "8m",
            "--init",
            "--log-driver",
            "none",
            "--restart",
            "no",
            "--stop-timeout",
            "1",
            "--env",
            "LANG=C.UTF-8",
            "--env",
            "PYTHONHASHSEED=0",
        ]
        for path, destination, readonly in mounts:
            args += self.mount(path, destination, readonly)
        if mode == "install":
            args += [
                "--tmpfs",
                "/deps:rw,noexec,nosuid,nodev,size=64m,nr_inodes=4096,uid=65532,gid=65532,mode=0700",
            ]
        args += [self.image, "python", "-I", "-B", "/opt/runner/bootstrap.py", mode]
        started = time.monotonic()
        stdout, stderr = bytearray(), bytearray()
        cid, proc, state, error, cleanup = None, None, {}, None, False
        actual = {}
        try:
            cid = self.command(args).stdout.decode().strip()
            info = json.loads(self.command(["inspect", cid]).stdout)[0]
            config, host = info["Config"], info["HostConfig"]
            actual = {
                "network": host["NetworkMode"],
                "read_only": host["ReadonlyRootfs"],
                "user": config["User"],
                "memory_bytes": host["Memory"],
                "memory_swap_bytes": host["MemorySwap"],
                "pids": host["PidsLimit"],
                "nano_cpus": host["NanoCpus"],
                "cap_drop": host["CapDrop"],
                "security_opt": host["SecurityOpt"],
                "apparmor": info["AppArmorProfile"],
                "image": info["Image"],
                "pids_limit": host["PidsLimit"],
                "cpus": host["NanoCpus"] / 1_000_000_000,
                "timeout_seconds": timeout,
                "no_new_privileges": "no-new-privileges:true" in host["SecurityOpt"],
            }
            if mode == "install":
                setting = host.get("Tmpfs", {}).get("/deps", "")
                if (
                    "size=64m" not in setting
                    or "nr_inodes=4096" not in setting
                    or any(m.get("Type") == "bind" and m.get("RW") for m in info["Mounts"])
                ):
                    raise RunnerError("install_filesystem_policy_mismatch")
                actual["install_tmpfs_bytes"] = 64 * 1024 * 1024
                actual["install_tmpfs_inodes"] = 4096
            if (
                actual["network"] != "none"
                or not actual["read_only"]
                or actual["user"] != "65532:65532"
                or actual["image"] != self.image
                or actual["memory_bytes"] != memory
                or actual["memory_swap_bytes"] != memory
                or actual["pids"] != 32
                or actual["nano_cpus"] != 1_000_000_000
                or actual["cap_drop"] != ["ALL"]
                or "no-new-privileges:true" not in actual["security_opt"]
                or actual["apparmor"] != "docker-default"
            ):
                raise RunnerError("container_policy_mismatch")
            proc = subprocess.Popen(
                ["docker", "start", "--attach", cid],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            selector = selectors.DefaultSelector()
            for stream, label in [(proc.stdout, "stdout"), (proc.stderr, "stderr")]:
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, label)
            try:
                while selector.get_map():
                    if cancelled():
                        error = "cancelled"
                        break
                    if time.monotonic() - started > timeout:
                        error = "timeout"
                        break
                    for key, _ in selector.select(0.1):
                        block = os.read(key.fd, 16_384)
                        if not block:
                            selector.unregister(key.fileobj)
                            continue
                        target = stdout if key.data == "stdout" else stderr
                        cap = (
                            (INSTALL_ARCHIVE_BYTES if mode == "install" else LIMITS["stdout_bytes"])
                            if key.data == "stdout"
                            else LIMITS["stderr_bytes"]
                        )
                        if len(target) + len(block) > cap:
                            target.extend(block[: max(0, cap - len(target))])
                            error = "output_limit"
                            break
                        target.extend(block)
                    if error:
                        break
                if error:
                    # Close the attach transport before waiting on daemon kill. Otherwise
                    # a full stdout pipe can keep the daemon's copy goroutine blocked.
                    if proc.poll() is None:
                        os.killpg(proc.pid, signal.SIGKILL)
                        proc.wait(timeout=3)
                    self.command(["kill", "--signal", "KILL", cid], timeout=5, check=False)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    error = error or "docker_attach_stuck"
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait(timeout=5)
                    self.command(["kill", "--signal", "KILL", cid], timeout=5, check=False)
            finally:
                selector.close()
                proc.stdout.close()
                proc.stderr.close()
            state = json.loads(self.command(["inspect", "--format", "{{json .State}}", cid]).stdout)
            if state.get("Running"):
                error = error or "unexpected_running_state"
                self.command(["kill", "--signal", "KILL", cid], check=False)
        except RunnerError as exc:
            error = error or exc.code
        except Exception:
            error = error or "runner_execution_error"
        finally:
            # Removing the whole namespace also kills detached/setsid descendants.
            removed = self.command(["rm", "--force", cid or name], check=False)
            absent = self.command(["inspect", cid or name], check=False)
            cleanup = absent.returncode != 0 and (removed.returncode == 0 or cid is None)
            if proc and proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=5)
        if not cleanup:
            error = "cleanup_failed"
        return {
            "stdout": bytes(stdout),
            "stderr": bytes(stderr),
            "container_id": cid,
            "runtime_sha256": self.image,
            "actual_limits": actual,
            "exit_code": state.get("ExitCode"),
            "oom_killed": bool(state.get("OOMKilled")),
            "error_code": error,
            "cleanup": cleanup,
            "duration_ms": round((time.monotonic() - started) * 1000),
            "raw_stdout_sha256": hashlib.sha256(stdout).hexdigest(),
            "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
        }

    def cleanup_owned(self):
        ids = (
            self.command(["ps", "--all", "--quiet", "--filter", f"label={OWNER}={self.scope}"])
            .stdout.decode()
            .split()
        )
        for cid in ids:
            self.command(["rm", "--force", cid])
        return len(ids)
