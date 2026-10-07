"""Read-only post-freeze verification inside the real API and Worker containers."""

import argparse
import hmac
import json
from pathlib import Path
import subprocess
import sys

from prepare_control_network import (
    ALIAS,
    LABEL,
    command,
    inspect_container,
    private_env,
    service_container,
    snapshot,
)


PROBE = r"""
import json,os,urllib.request,urllib.error
from pathlib import Path
from app.adapter_runner_client import AdapterRunnerClient
assert not Path('/var/run/docker.sock').exists()
assert not Path('/run/docker.sock').exists()
client=AdapterRunnerClient()
health=client.health()
assert health['status']=='ready' and health['runtime_sha256']==os.environ['ADAPTER_RUNNER_RUNTIME_DIGEST']
try:
    urllib.request.build_opener(urllib.request.ProxyHandler({})).open(os.environ['ADAPTER_RUNNER_URL']+'/v1/health',timeout=5)
    raise AssertionError('Anonymous control request was accepted')
except urllib.error.HTTPError as error:
    assert error.code==401
print(json.dumps({'first_party_client_health':health,'anonymous_status':401,'docker_socket_absent':True,'model_calls':0}))
"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--main-dir", required=True, type=Path)
    parser.add_argument("--runner-project", default="hr-adapter-runner-verify-20261005")
    parser.add_argument("--expected-main-image")
    parser.add_argument(
        "--output", type=Path, default=Path("verification-main-control-reachable.json")
    )
    args = parser.parse_args()
    if sys.platform != "linux":
        raise SystemExit("Run only on the authorized remote Linux Docker host")
    runner_dir = Path(__file__).resolve().parent
    main_dir = args.main_dir.resolve()
    values = private_env(main_dir / "deploy/server.env")
    project = values["COMPOSE_PROJECT_NAME"]
    network = values["ADAPTER_RUNNER_CONTROL_NETWORK"]
    broker = service_container(args.runner_project, "broker", runner_dir)
    broker_values = dict(item.split("=", 1) for item in broker["Config"]["Env"] if "=" in item)
    runtime = broker_values["RUNNER_RUNTIME_IMAGE"]
    if any(broker["NetworkSettings"]["Ports"].values()):
        raise RuntimeError(
            "Broker still publishes a host port; apply its control overlay after the freeze"
        )
    actual_network = json.loads(command(["docker", "network", "inspect", network]))[0]
    if not actual_network["Internal"] or actual_network.get("Labels", {}).get(LABEL) != project:
        raise RuntimeError("Dedicated network isolation/ownership does not match")
    results, expected_ids = {}, {broker["Id"]}
    for name in ["api", "worker"]:
        info = service_container(project, name, main_dir)
        expected_ids.add(info["Id"])
        if args.expected_main_image and info["Image"] != args.expected_main_image:
            raise RuntimeError("A service does not use the frozen business image")
        if (
            info["HostConfig"]["NetworkMode"] == "host"
            or network not in info["NetworkSettings"]["Networks"]
        ):
            raise RuntimeError("API/Worker must use the dedicated bridge, not host networking")
        if any(m["Destination"].endswith("docker.sock") for m in info["Mounts"]):
            raise RuntimeError("Application Docker socket mount detected")
        environment = dict(item.split("=", 1) for item in info["Config"]["Env"] if "=" in item)
        if (
            environment.get("ADAPTER_RUNNER_URL") != "http://" + ALIAS + ":8090"
            or environment.get("ADAPTER_RUNNER_RUNTIME_DIGEST") != runtime
        ):
            raise RuntimeError(
                "Application fixed Runner URL/runtime does not match the live Broker"
            )
        if not hmac.compare_digest(
            environment.get("ADAPTER_RUNNER_TOKEN", ""), broker_values["RUNNER_TOKEN"]
        ):
            raise RuntimeError("Application Runner token does not match the live Broker")
        process = subprocess.run(
            ["docker", "exec", "-i", info["Id"], "python", "-B", "-"],
            input=PROBE,
            text=True,
            capture_output=True,
            timeout=30,
        )
        if process.returncode:
            raise RuntimeError(
                "Real application first-party client probe failed; private output suppressed"
            )
        results[name] = {"container": snapshot(info), "probe": json.loads(process.stdout)}
    if set(actual_network.get("Containers", {})) != expected_ids:
        raise RuntimeError("The dedicated control network contains extra or missing services")
    if any(
        name.startswith(project + "_") and name != network
        for name in broker["NetworkSettings"]["Networks"]
    ):
        raise RuntimeError("Broker must not join application data/app networks")
    other_services_with_token = []
    for cid in command(
        ["docker", "ps", "-aq", "--filter", "label=com.docker.compose.project=" + project]
    ).split():
        info = inspect_container(cid)
        name = info["Config"]["Labels"].get("com.docker.compose.service")
        if name not in {"api", "worker"}:
            env = dict(item.split("=", 1) for item in info["Config"]["Env"] if "=" in item)
            if env.get("ADAPTER_RUNNER_TOKEN"):
                other_services_with_token.append(name)
    if other_services_with_token:
        raise RuntimeError(
            "Runner token was also injected into other application services: "
            + ",".join(other_services_with_token)
        )
    baseline = json.loads((runner_dir / "verification-control-network-before.json").read_text())
    for previous in baseline:
        info = inspect_container(previous["id"])
        if {
            "id": info["Id"],
            "name": info["Name"],
            "started_at": info["State"]["StartedAt"],
        } != previous:
            raise RuntimeError("An unrelated container changed since network preparation")
    result = {
        "status": "passed",
        "control_network": network,
        "internal": True,
        "runtime_sha256": runtime,
        "broker_image": broker["Image"],
        "broker_host_ports": [],
        "services": results,
        "unrelated_containers_unchanged": len(baseline),
        "extra_services_with_runner_token": [],
        "host_network_used": False,
        "candidate_runs_created": 0,
        "llm_calls": 0,
        "notifications": 0,
    }
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error_type": type(exc).__name__,
                    "reason": str(exc)
                    if isinstance(exc, RuntimeError)
                    else "Private diagnostics suppressed",
                }
            )
        )
        raise SystemExit(1) from None
