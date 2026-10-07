"""Run only on the authorized Docker host. All candidates are controlled fixtures."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import hmac
import json
from pathlib import Path
import subprocess
import time
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import ProxyHandler, Request, build_opener
import uuid

from common import canonical, digest

TERMINAL = {"succeeded", "failed", "cancelled", "unknown"}


class Verification:
    def __init__(self, state_name="verification-state.json"):
        values = dict(
            line.split("=", 1)
            for line in Path("runner.env").read_text().splitlines()
            if "=" in line
        )
        self.token = values["RUNNER_TOKEN"]
        self.path = Path(state_name)
        self.state = (
            json.loads(self.path.read_text())
            if self.path.exists()
            else {"prefix": uuid.uuid4().hex, "checks": [], "runs": {}, "locks": {}}
        )
        self.save()

    def save(self):
        self.path.write_text(json.dumps(self.state, ensure_ascii=False, indent=2))

    def api(self, method, path, body=None, expected=200, authenticated=True):
        headers = {"Content-Type": "application/json"}
        if authenticated:
            headers["Authorization"] = "Bearer " + self.token
        request = Request(
            "http://127.0.0.1:18082" + path,
            data=canonical(body) if body is not None else None,
            headers=headers,
            method=method,
        )
        try:
            with build_opener(ProxyHandler({})).open(request, timeout=110) as response:
                code, raw = response.status, response.read(5_000_001)
        except HTTPError as exc:
            code, raw = exc.code, exc.read(5_000_001)
        assert code in (expected if isinstance(expected, tuple) else (expected,)), (
            method,
            path,
            code,
            raw[:500].decode(errors="replace"),
        )
        assert len(raw) <= 5_000_000
        return json.loads(raw)

    def check(self, name, function):
        if name in self.state["checks"]:
            return
        function()
        self.state["checks"].append(name)
        self.save()
        print(json.dumps({"passed": name}, ensure_ascii=False), flush=True)

    def lock(self, dependencies):
        key = digest(dependencies)
        if key not in self.state["locks"]:
            result = self.api("POST", "/v1/dependencies/resolve", {"dependencies": dependencies})
            assert digest(result["lock"]) == result["lock_sha256"]
            self.state["locks"][key] = result["lock"]
            self.save()
        return self.state["locks"][key]

    def prepare(
        self, name, source, dependencies=None, payload=None, operation="parse_list", helpers=None
    ):
        dependencies = dependencies or []
        lock = self.lock(dependencies)
        bundle = {
            "schema_version": 1,
            "contract": "python_adapter_v1",
            "kind": "boss",
            "files": {"adapter.py": source, **(helpers or {})},
            "dependencies": dependencies,
            "lock": lock,
        }
        self.api(
            "PUT",
            "/v1/bundles/" + digest(bundle),
            bundle,
            expected=(200, 201),
        )
        request = {
            "idempotency_key": self.state["prefix"] + ":" + name,
            "bundle_sha256": digest(bundle),
            "operation": operation,
            "input": payload or {},
        }
        if name not in self.state["runs"]:
            # Persist the exact key/request before any possibly ambiguous POST.
            self.state["runs"][name] = {"request": request, "lock_sha256": digest(lock)}
            self.save()
        saved = self.state["runs"][name]
        assert saved["request"] == request
        if "run_id" not in saved:
            state = self.api("POST", "/v1/runs", request, expected=202)
            saved["run_id"] = state["run_id"]
            self.save()
        return saved

    def wait(self, saved):
        deadline = time.monotonic() + 100
        while time.monotonic() < deadline:
            result = self.api("GET", "/v1/runs/" + saved["run_id"])
            if result["status"] in TERMINAL:
                self.verify_receipt(saved, result)
                saved["result"] = result
                self.save()
                return result
            time.sleep(0.3)
        raise AssertionError("Existing run remains active; inspect without recreating it")

    def verify_receipt(self, saved, result):
        receipt = result["receipt"]
        signature = receipt["signature"]
        unsigned = {key: value for key, value in receipt.items() if key != "signature"}
        key = hmac.new(
            self.token.encode(), b"hr-adapter-runner-receipt-v1", hashlib.sha256
        ).digest()
        assert hmac.compare_digest(
            signature, hmac.new(key, canonical(unsigned), hashlib.sha256).hexdigest()
        )
        assert receipt["run_id"] == saved["run_id"] and receipt["status"] == result["status"]
        assert receipt["request_sha256"] == digest(saved["request"])
        assert receipt["input_sha256"] == digest(saved["request"]["input"])
        assert receipt["bundle_sha256"] == saved["request"]["bundle_sha256"]
        assert receipt["lock_sha256"] == saved["lock_sha256"]
        assert (
            receipt["cleanup"]["container_removed"]
            and receipt["cleanup"]["process_group_terminated"]
        )
        assert (
            receipt["dependency_environment_sha256"] and receipt["dependency_installation_sha256"]
        )
        if result["status"] == "succeeded":
            assert receipt["output_sha256"] == digest(result["output"])
            for name in ["limits", "actual_limits"]:
                policy = receipt[name]
                assert (
                    policy["network"] == "none"
                    and policy["read_only"]
                    and policy["user"] == "65532:65532"
                )
                assert policy["no_new_privileges"] and policy["cap_drop"] == ["ALL"]
                assert policy["pids_limit"] == 32 and policy["memory_bytes"] == 268435456
                assert policy["cpus"] == 1 and policy["timeout_seconds"] == 30


def main():
    v = Verification()
    for attempt in range(50):
        try:
            if v.api("GET", "/v1/health")["status"] == "ready":
                break
        except Exception:
            if attempt == 49:
                raise
        time.sleep(0.2)
    v.check("authentication", lambda: v.api("GET", "/v1/health", expected=401, authenticated=False))
    health = v.api("GET", "/v1/health")
    assert health["status"] == "ready" and health["contract"] == "python_adapter_v1"
    v.state["runtime_sha256"] = health["runtime_sha256"]
    v.save()

    def invalid_dependencies():
        for dependencies in [
            ["requests==2.32.0"],
            ["six>=1"],
            ["six @ https://example.invalid/x.whl"],
        ]:
            v.api("POST", "/v1/dependencies/resolve", {"dependencies": dependencies}, expected=400)

    v.check("unapproved_dependency_rejection", invalid_dependencies)

    def isolation():
        source = """def run(operation, payload):
    import os, socket
    from pathlib import Path
    blocked = {}
    for path in ['/bundle/adapter.py', '/deps/forbidden-write', '/root/forbidden-write']:
        try:
            with open(path, 'a') as f: f.write('must-not-write')
            blocked[path] = False
        except OSError:
            blocked[path] = True
    network = {}
    for address in ['1.1.1.1', '10.255.200.16']:
        s = socket.socket(); s.settimeout(0.5)
        try: s.connect((address, 443)); network[address] = False
        except OSError: network[address] = True
        finally: s.close()
    status = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line)
    return {'uid': os.getuid(), 'blocked': blocked, 'network': network,
        'docker_socket_absent': not Path('/var/run/docker.sock').exists(),
        'broker_data_absent': not Path('/data/state.sqlite3').exists(),
        'token_absent': 'RUNNER_TOKEN' not in os.environ,
        'caps': status['CapEff'].strip(), 'no_new_privs': status['NoNewPrivs'].strip(),
        'seccomp': status['Seccomp'].strip(), 'pids_max': Path('/sys/fs/cgroup/pids.max').read_text().strip(),
        'memory_max': Path('/sys/fs/cgroup/memory.max').read_text().strip()}
"""
        result = v.wait(v.prepare("isolation", source))
        assert result["status"] == "succeeded", result["error_code"]
        out = result["output"]
        assert out["uid"] == 65532 and all(out["blocked"].values()) and all(out["network"].values())
        assert out["docker_socket_absent"] and out["broker_data_absent"] and out["token_absent"]
        assert (
            out["caps"] == "0000000000000000"
            and out["no_new_privs"] == "1"
            and out["seccomp"] == "2"
        )
        assert out["pids_max"] == "32" and out["memory_max"] == "268435456"

    v.check("real_container_isolation", isolation)

    source = """def run(operation, payload):
    import bs4
    from bs4 import BeautifulSoup
    from lib.helper import suffix
    soup = BeautifulSoup(payload['html'], 'html.parser')
    return {'title': soup.select_one('h1').get_text(strip=True) + suffix, 'dependency_version': bs4.__version__}
"""
    helpers = {"lib/helper.py": "suffix = ' / fixture'"}

    def dependency_versions():
        for version in ["4.13.4", "4.13.5"]:
            saved = v.prepare(
                "dependency-" + version,
                source,
                ["beautifulsoup4==" + version],
                {"html": "<h1>平台工程师</h1>"},
                helpers=helpers,
            )
            result = v.wait(saved)
            assert result["status"] == "succeeded", result["error_code"]
            assert result["output"] == {
                "title": "平台工程师 / fixture",
                "dependency_version": version,
            }
        first = v.state["runs"]["dependency-4.13.4"]
        second = v.state["runs"]["dependency-4.13.5"]
        assert first["lock_sha256"] != second["lock_sha256"]
        # Read and invoke the old immutable artifact again, as a rollback consumer would.
        result = v.wait(
            v.prepare(
                "rollback-old-artifact",
                source,
                ["beautifulsoup4==4.13.4"],
                {"html": "<h1>旧版本仍可运行</h1>"},
                helpers=helpers,
            )
        )
        assert result["output"]["dependency_version"] == "4.13.4"

    v.check("two_dependency_versions_and_old_artifact", dependency_versions)

    def idempotency():
        saved = v.state["runs"]["dependency-4.13.5"]
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(
                pool.map(
                    lambda _: v.api("POST", "/v1/runs", saved["request"], expected=202), range(12)
                )
            )
        assert {result["run_id"] for result in results} == {saved["run_id"]}
        looked = v.api(
            "GET", "/v1/runs/by-key/" + quote(saved["request"]["idempotency_key"], safe="")
        )
        assert looked["run_id"] == saved["run_id"]
        v.api("POST", "/v1/runs", {**saved["request"], "input": {"different": True}}, expected=409)

    v.check("concurrent_idempotency_and_by_key_lookup", idempotency)

    def navigation():
        saved = v.prepare(
            "navigation",
            "def run(operation, payload):\n    return {'next_url': payload['observed_url'], 'operation': operation}\n",
            payload={"observed_url": "https://example.invalid/page/2"},
            operation="navigation",
        )
        result = v.wait(saved)
        assert result["status"] == "succeeded" and result["output"]["operation"] == "navigation"

    v.check("navigation_returns_data_only", navigation)

    def output_limit():
        saved = v.prepare(
            "output-limit-v2",
            "def run(operation, payload):\n    import os\n    while True: os.write(1, b'x' * 65536)\n",
        )
        result = v.wait(saved)
        assert result["status"] == "failed" and result["error_code"] == "output_limit"

    v.check("output_limit_kills_container", output_limit)

    def memory_limit():
        saved = v.prepare(
            "memory-limit",
            "def run(operation, payload):\n    value = bytearray(400 * 1024 * 1024)\n    return {'bytes': len(value)}\n",
        )
        result = v.wait(saved)
        assert result["status"] == "failed" and result["receipt"]["oom_killed"]

    v.check("memory_limit_and_oom_evidence", memory_limit)

    def pids_limit():
        source = """def run(operation, payload):
    import os, signal, time
    children = []; blocked = False
    try:
        for _ in range(40):
            try: pid = os.fork()
            except OSError: blocked = True; break
            if pid == 0:
                time.sleep(60); os._exit(0)
            children.append(pid)
    finally:
        for pid in children:
            os.kill(pid, signal.SIGKILL); os.waitpid(pid, 0)
    return {'blocked': blocked, 'children': len(children)}
"""
        result = v.wait(v.prepare("pids-limit", source))
        assert (
            result["status"] == "succeeded"
            and result["output"]["blocked"]
            and result["output"]["children"] <= 30
        )

    v.check("pids_limit", pids_limit)

    def timeout():
        source = """def run(operation, payload):
    import subprocess, sys, time
    subprocess.Popen([sys.executable, '-c', 'import os,time; os.setsid(); time.sleep(600)'])
    while True: time.sleep(1)
"""
        result = v.wait(v.prepare("timeout-descendant", source))
        assert result["status"] == "failed" and result["error_code"] == "timeout"

    v.check("timeout_removes_detached_descendants", timeout)

    def cancelled():
        saved = v.prepare(
            "cancel",
            "def run(operation, payload):\n    import time\n    time.sleep(600)\n    return {}\n",
        )
        v.api("DELETE", "/v1/runs/" + saved["run_id"])
        result = v.wait(saved)
        assert result["status"] == "cancelled"

    v.check("explicit_cancel", cancelled)

    def restart_unknown():
        saved = v.prepare(
            "restart-unknown",
            "def run(operation, payload):\n    import time\n    time.sleep(600)\n    return {}\n",
        )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            ids = subprocess.check_output(
                [
                    "docker",
                    "ps",
                    "-q",
                    "--filter",
                    "label=org.hr-research.adapter-runner=hr-adapter-runner-verify-20261005",
                ],
                text=True,
            ).split()
            if ids:
                break
            time.sleep(0.2)
        assert ids
        subprocess.run(
            ["docker", "compose", "--env-file", "runner.env", "restart", "broker"],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        for _ in range(60):
            try:
                if v.api("GET", "/v1/health")["status"] == "ready":
                    break
            except Exception:
                pass
            time.sleep(0.2)
        result = v.wait(saved)
        assert result["status"] == "unknown" and result["error_code"] == "broker_restarted"
        duplicate = v.api("POST", "/v1/runs", saved["request"], expected=202)
        assert duplicate["run_id"] == saved["run_id"] and duplicate["status"] == "unknown"

    v.check("broker_restart_unknown_not_reexecuted", restart_unknown)

    leftovers = subprocess.check_output(
        [
            "docker",
            "ps",
            "-aq",
            "--filter",
            "label=org.hr-research.adapter-runner=hr-adapter-runner-verify-20261005",
        ],
        text=True,
    ).split()
    assert not leftovers
    before = json.loads(Path("containers-before.json").read_text())
    after = [
        subprocess.check_output(
            [
                "docker",
                "inspect",
                "--format",
                "{{.Id}} {{.Name}} {{.State.StartedAt}}",
                line.split()[0],
            ],
            text=True,
        ).strip()
        for line in before
    ]
    assert before == after
    v.state.update(
        {
            "other_containers_unchanged": len(before),
            "execution_containers_remaining": 0,
            "real_llm_calls": 0,
            "real_notifications": 0,
            "unknown_candidates_executed": 0,
        }
    )
    v.save()
    print(
        json.dumps(
            {key: value for key, value in v.state.items() if key not in {"runs", "locks"}},
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
