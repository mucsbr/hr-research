"""Authorized Linux host only: prepare transport, never upgrade the business stack."""

import argparse
import hmac
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import uuid

LABEL = "org.hr-research.runner-control.owner"
ALIAS = "adapter-runner-broker"


def command(args, *, cwd=None, timeout=30):
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError("Control-network command failed; private command output was suppressed")
    return result.stdout


def private_env(path):
    if path.is_symlink() or not path.is_file() or stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise RuntimeError("Expected a private regular environment file")
    return dict(
        line.split("=", 1)
        for line in path.read_text().splitlines()
        if line and not line.startswith("#") and "=" in line
    )


def update_private_env(path, values):
    private_env(path)
    lines, seen = [], set()
    for line in path.read_text().splitlines():
        key = line.split("=", 1)[0] if "=" in line and not line.startswith("#") else None
        if key in values:
            if key not in seen:
                lines.append(key + "=" + values[key])
                seen.add(key)
        else:
            lines.append(line)
    lines += [key + "=" + value for key, value in values.items() if key not in seen]
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w") as output:
            output.write("\n".join(lines) + "\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def inspect_container(cid):
    return json.loads(command(["docker", "inspect", cid]))[0]


def service_container(project, service, directory):
    ids = command(
        [
            "docker",
            "ps",
            "-q",
            "--filter",
            "label=com.docker.compose.project=" + project,
            "--filter",
            "label=com.docker.compose.service=" + service,
        ]
    ).split()
    if len(ids) != 1:
        raise RuntimeError("Expected exactly one running service in the authorized project")
    info = inspect_container(ids[0])
    if (
        Path(info["Config"]["Labels"]["com.docker.compose.project.working_dir"]).resolve()
        != directory
    ):
        raise RuntimeError("Service belongs to a different checkout")
    return info


def snapshot(info):
    return {
        "id": info["Id"],
        "name": info["Name"],
        "image": info["Image"],
        "started_at": info["State"]["StartedAt"],
        "networks": sorted(info["NetworkSettings"]["Networks"]),
        "mount_destinations": sorted(m["Destination"] for m in info["Mounts"]),
    }


PROBE = r"""
import json,os,sys,urllib.request,urllib.error
from pathlib import Path
values=dict(line.split('=',1) for line in Path('/config/runner.env').read_text().splitlines() if '=' in line)
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
url='http://adapter-runner-broker:8090/v1/health'
try:
    try:
        opener.open(url,timeout=5)
        raise AssertionError('Unauthenticated request was accepted')
    except urllib.error.HTTPError as exc:
        assert exc.code==401
    request=urllib.request.Request(url,headers={'Authorization':'Bearer '+values['RUNNER_TOKEN']})
    with opener.open(request,timeout=5) as response: health=json.load(response)
    assert health['status']=='ready' and health['contract']=='python_adapter_v1'
    assert health['runtime_sha256']==sys.argv[1]
    assert not Path('/var/run/docker.sock').exists() and not Path('/data').exists()
    print(json.dumps({'authenticated_health':health,'anonymous_status':401,'docker_socket_absent':True,'application_data_absent':True,'probe_uid':os.getuid(),'transport':'dedicated-internal-bridge'}))
except Exception as exc:
    print(json.dumps({'probe_error_type':type(exc).__name__}))
    raise SystemExit(1)
"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--main-dir", type=Path, required=True)
    parser.add_argument("--runner-project", default="hr-adapter-runner-verify-20261005")
    parser.add_argument("--output", type=Path, default=Path("verification-control-network.json"))
    args = parser.parse_args()
    if sys.platform != "linux":
        raise SystemExit("Run only on the authorized remote Linux Docker host; no local Docker use")
    runner_dir = Path(__file__).resolve().parent
    main_dir = args.main_dir.resolve()
    runner_env, main_env = runner_dir / "runner.env", main_dir / "deploy/server.env"
    runner_values, main_values = private_env(runner_env), private_env(main_env)
    project = main_values.get("COMPOSE_PROJECT_NAME", "")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,62}", project):
        raise RuntimeError("Main deployment must have an explicit valid Compose project")
    broker = service_container(args.runner_project, "broker", runner_dir)
    api = service_container(project, "api", main_dir)
    worker = service_container(project, "worker", main_dir)
    live = dict(value.split("=", 1) for value in broker["Config"]["Env"] if "=" in value)
    runtime = live.get("RUNNER_RUNTIME_IMAGE", "")
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", runtime) or not hmac.compare_digest(
        live.get("RUNNER_TOKEN", ""), runner_values.get("RUNNER_TOKEN", "")
    ):
        raise RuntimeError("Private Runner configuration does not match the live Broker")
    if command(["docker", "image", "inspect", "--format", "{{.Id}}", runtime]).strip() != runtime:
        raise RuntimeError("Live fixed runtime image was not found")
    for info in [api, worker]:
        if any(m["Destination"].endswith("docker.sock") for m in info["Mounts"]):
            raise RuntimeError("Application services must not mount Docker socket")
    all_infos = [inspect_container(cid) for cid in command(["docker", "ps", "-q"]).split()]
    unchanged = [snapshot(info) for info in all_infos if info["Id"] != broker["Id"]]
    baseline_path = runner_dir / "verification-control-network-before.json"
    if not baseline_path.exists():
        outside = [
            {"id": info["Id"], "name": info["Name"], "started_at": info["State"]["StartedAt"]}
            for info in all_infos
            if info["Config"].get("Labels", {}).get("com.docker.compose.project")
            not in {project, args.runner_project}
        ]
        baseline_path.write_text(json.dumps(outside, indent=2))
    network = project + "_runner_control"
    subnet = str(
        ipaddress.IPv4Network(
            main_values.get("ADAPTER_RUNNER_CONTROL_SUBNET", "172.16.243.0/24")
        )
    )
    if not ipaddress.IPv4Network(subnet).subnet_of(ipaddress.IPv4Network("172.16.0.0/12")):
        raise RuntimeError("Runner control subnet must stay within private 172.16.0.0/12")
    existing = command(
        ["docker", "network", "ls", "--filter", "name=^" + network + "$", "--format", "{{.ID}}"]
    ).split()
    if existing:
        spec = json.loads(command(["docker", "network", "inspect", network]))[0]
        if (
            not spec["Internal"]
            or spec["Driver"] != "bridge"
            or spec.get("Labels", {}).get(LABEL) != project
            or [item.get("Subnet") for item in spec.get("IPAM", {}).get("Config", [])]
            != [subnet]
        ):
            raise RuntimeError(
                "Refusing an existing network with different ownership, isolation or subnet; "
                "stop this project's services and recreate only its unused network"
            )
    else:
        command(
            [
                "docker",
                "network",
                "create",
                "--driver",
                "bridge",
                "--internal",
                "--subnet",
                subnet,
                "--label",
                LABEL + "=" + project,
                network,
            ]
        )
        spec = json.loads(command(["docker", "network", "inspect", network]))[0]
    allowed = {api["Id"], worker["Id"], broker["Id"]}
    if set(spec.get("Containers", {})) - allowed:
        raise RuntimeError("The dedicated control network already contains an unrelated container")
    if network not in broker["NetworkSettings"]["Networks"]:
        command(["docker", "network", "connect", "--alias", ALIAS, network, broker["Id"]])
    elif ALIAS not in (broker["NetworkSettings"]["Networks"][network].get("Aliases") or []):
        raise RuntimeError(
            "Existing Broker endpoint lacks the fixed alias; not interrupting it automatically"
        )
    probe_name = "hr-runner-control-probe-" + uuid.uuid4().hex[:12]
    identity = runner_env.stat()
    try:
        raw = command(
            [
                "docker",
                "run",
                "--rm",
                "--name",
                probe_name,
                "--network",
                network,
                "--read-only",
                "--user",
                f"{identity.st_uid}:{identity.st_gid}",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges:true",
                "--pids-limit",
                "32",
                "--memory",
                "128m",
                "--cpus",
                "1",
                "--tmpfs",
                "/tmp:size=16m,mode=1777",
                "--mount",
                f"type=bind,src={runner_env},dst=/config/runner.env,readonly",
                "--entrypoint",
                "python",
                runtime,
                "-B",
                "-c",
                PROBE,
                runtime,
            ],
            timeout=30,
        )
        proof = json.loads(raw)
    finally:
        subprocess.run(
            ["docker", "rm", "--force", probe_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    updates = {
        "ADAPTER_RUNNER_URL": "http://" + ALIAS + ":8090",
        "ADAPTER_RUNNER_TOKEN": runner_values["RUNNER_TOKEN"],
        "ADAPTER_RUNNER_RUNTIME_DIGEST": runtime,
        "ADAPTER_RUNNER_TIMEOUT_SECONDS": "90",
        "ADAPTER_RUNNER_CONTROL_NETWORK": network,
    }
    update_private_env(main_env, updates)
    update_private_env(runner_env, {"ADAPTER_RUNNER_CONTROL_NETWORK": network})
    main_config = json.loads(
        command(
            [
                "docker",
                "compose",
                "--env-file",
                str(main_env),
                "-f",
                str(main_dir / "compose.yaml"),
                "-f",
                str(main_dir / "compose.code-maintenance.yaml"),
                "config",
                "--format",
                "json",
            ],
            cwd=main_dir,
        )
    )
    pending = {}
    for service in ["api", "worker"]:
        item = main_config["services"][service]
        if set(item["networks"]) != {"app", "data", "adapter_control"}:
            raise RuntimeError("Application network merge did not preserve app/data/control")
        pending[service] = [
            name
            for name, value in updates.items()
            if name != "ADAPTER_RUNNER_CONTROL_NETWORK"
            and str(item.get("environment", {}).get(name, "")) != value
        ]
    if any(
        "adapter_control" in item.get("networks", {})
        for name, item in main_config["services"].items()
        if name not in {"api", "worker"}
    ):
        raise RuntimeError("Another application service would join the control network")
    runner_config = json.loads(
        command(
            [
                "docker",
                "compose",
                "--env-file",
                str(runner_env),
                "-f",
                str(runner_dir / "compose.yaml"),
                "-f",
                str(runner_dir / "compose.control.yaml"),
                "config",
                "--format",
                "json",
            ],
            cwd=runner_dir,
        )
    )
    if runner_config["services"]["broker"].get("ports"):
        raise RuntimeError("Production Broker overlay must not publish a host port")
    after = [snapshot(inspect_container(info["id"])) for info in unchanged]
    if after != unchanged:
        raise RuntimeError(
            "An existing application or unrelated container changed during preparation"
        )
    final_broker = inspect_container(broker["Id"])
    if (
        final_broker["State"]["StartedAt"] != broker["State"]["StartedAt"]
        or final_broker["Image"] != broker["Image"]
    ):
        raise RuntimeError("Broker was unexpectedly restarted or upgraded")
    result = {
        "phase": "prepared_not_business_deployed",
        "main_project": project,
        "runner_project": args.runner_project,
        "control_network": network,
        "internal": True,
        "broker_alias": ALIAS,
        "runtime_sha256": runtime,
        "broker_image": broker["Image"],
        "probe": proof,
        "main_api_before": snapshot(api),
        "main_worker_before": snapshot(worker),
        "existing_containers_unchanged": len(unchanged),
        "broker_not_restarted": True,
        "main_service_env_mapping_pending": pending,
        "private_config_permissions": "0600",
        "main_api_worker_live_control_attachment": all(
            network in info["NetworkSettings"]["Networks"] for info in [api, worker]
        ),
        "production_broker_publishes_host_ports": False,
        "secrets_printed": False,
        "llm_calls": 0,
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
                    else "Inspect the authorized project's configuration; private outputs suppressed",
                }
            )
        )
        raise SystemExit(1) from None
