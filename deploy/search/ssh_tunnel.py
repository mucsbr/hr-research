"""Idempotent loopback-only SSH access for local Conda; never calls local Docker.

Use start/status/stop. Re-run start after a remote container is recreated: its
current fixed-network IP is rediscovered before an existing tunnel is reused.
"""

import argparse
import ipaddress
import json
import os
from pathlib import Path
import shlex
import stat
import subprocess
import time
import uuid
from urllib.request import ProxyHandler, build_opener

REMOTE = "dabing@10.255.200.16"
CONTAINER = "hr-search-verify-20261005-core-1"
NETWORK = "hr-intel-verify-20261005_search_control"
IMAGE = "sha256:76b0bf285aca014c7191fc4d9234c4bfb358624ac33d8883833d496c059ec072"
ROOT = Path(__file__).resolve().parents[2]
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]


def command(args, *, check=True):
    result = subprocess.run(args, capture_output=True, text=True, timeout=20)
    if check and result.returncode:
        category = ("control_socket_path_too_long" if "too long" in result.stderr
                    else "local_port_in_use" if "Address already in use" in result.stderr
                    else "ssh_command_failed")
        raise RuntimeError(category + "; credentials and private diagnostics suppressed")
    return result


def remote_target():
    fmt = ('{"running":{{json .State.Running}},"health":{{json .State.Health.Status}},'
           '"image":{{json .Image}},"ip":{{json (index .NetworkSettings.Networks "' + NETWORK + '").IPAddress}}}')
    script = shlex.join(["docker", "inspect", "--format", fmt, CONTAINER])
    info = json.loads(command([*SSH, REMOTE, script]).stdout)
    address = ipaddress.ip_address(info["ip"])
    if (info["running"] is not True or info["health"] != "healthy" or info["image"] != IMAGE
            or address.version != 4 or not address.is_private or address.is_loopback or address.is_link_local):
        raise RuntimeError("Remote search identity or private-network address does not match")
    return {"remote": REMOTE, "container": CONTAINER, "network": NETWORK, "image": IMAGE, "ip": str(address)}


def owned_socket(path):
    if not path.exists():
        return False
    info = path.lstat()
    if info.st_uid != os.getuid() or not stat.S_ISSOCK(info.st_mode):
        raise RuntimeError("Refusing to use an unexpected SSH control path")
    return True


def alive(path):
    return owned_socket(path) and command([*SSH, "-S", str(path), "-O", "check", REMOTE], check=False).returncode == 0


def stop(path):
    if alive(path):
        command([*SSH, "-S", str(path), "-O", "exit", REMOTE])
        for _ in range(20):
            if not path.exists():
                break
            time.sleep(0.05)
    if owned_socket(path):
        path.unlink()


def health(port):
    opener = build_opener(ProxyHandler({}))
    with opener.open(f"http://127.0.0.1:{port}/healthz", timeout=5) as response:
        if response.status != 200:
            raise RuntimeError("Forwarded search service is not healthy")


def save(path, value):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["start", "status", "stop"])
    parser.add_argument("--port", type=int, default=18083)
    parser.add_argument("--state-dir", type=Path, default=ROOT / ".runtime" / "search-ssh")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        raise SystemExit("Use an unprivileged local TCP port")
    directory = args.state_dir.expanduser().absolute()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.lstat()
    if directory.is_symlink() or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise RuntimeError("Tunnel state directory must be private and owned by this user")
    socket = directory / f"c{args.port}"
    state = directory / f"state-{args.port}.json"
    # OpenSSH adds a random suffix before atomically replacing its control socket.
    # Leave space below macOS sockaddr_un's limit for that temporary pathname.
    if len(os.fsencode(socket)) > 80:
        raise RuntimeError("Choose a shorter private --state-dir for the SSH control socket")
    previous = None
    if state.exists():
        meta = state.lstat()
        if not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.getuid() or stat.S_IMODE(meta.st_mode) & 0o077:
            raise RuntimeError("Tunnel state must be a private regular file")
        previous = json.loads(state.read_text())
    endpoint = f"http://127.0.0.1:{args.port}/search"
    if args.action == "stop":
        stop(socket)
        state.unlink(missing_ok=True)
        print(json.dumps({"status": "stopped", "endpoint": endpoint, "local_docker_calls": 0}))
        return
    if args.action == "status" and not alive(socket):
        print(json.dumps({"status": "not_running", "endpoint": endpoint, "local_docker_calls": 0}))
        return
    target = remote_target()
    expected = {**target, "local_port": args.port, "endpoint": endpoint}
    if args.action == "status":
        if previous != expected:
            print(json.dumps({"status": "target_changed", "action": "run start to refresh", "endpoint": endpoint}))
            return
        health(args.port)
        print(json.dumps({"status": "healthy", "endpoint": endpoint, "bind_address": "127.0.0.1", "upstream_queries": 0}))
        return
    reused = alive(socket) and previous == expected
    if not reused:
        stop(socket)
        command([*SSH, "-M", "-S", str(socket), "-o", "ControlPersist=no",
                 "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=15",
                 "-o", "ServerAliveCountMax=3", "-fNT", "-L",
                 f"127.0.0.1:{args.port}:{target['ip']}:8080", REMOTE])
        try:
            health(args.port)
            save(state, expected)
        except Exception:
            stop(socket)
            raise
    else:
        health(args.port)
    print(json.dumps({"status": "healthy", "reused": reused, "endpoint": endpoint,
                      "bind_address": "127.0.0.1", "remote_container_ip": target["ip"],
                      "server_host_port_published": False, "local_docker_calls": 0, "upstream_queries": 0}))


if __name__ == "__main__":
    main()
