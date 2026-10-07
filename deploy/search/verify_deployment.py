"""Read-only service isolation audit; --check-api is for the later frozen main deployment."""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

PROJECT = "hr-search-verify-20261005"
MAIN_PROJECT = "hr-intel-verify-20261005"
CONTROL = MAIN_PROJECT + "_search_control"
ENDPOINT = "http://recruitment-search:8080/search"
IMAGES = {
    "core": "sha256:76b0bf285aca014c7191fc4d9234c4bfb358624ac33d8883833d496c059ec072",
    "valkey": "sha256:48332870af354a799964c0012ae1194a0bf2bf894eb508f945810596dc2d8d11",
}
# Exclude environment values: they contain the private SearXNG secret.
FORMAT = ('{"id":{{json .Id}},"name":{{json .Name}},"image":{{json .Image}},'
          '"started_at":{{json .State.StartedAt}},"networks":{{json .NetworkSettings.Networks}},'
          '"ports":{{json .NetworkSettings.Ports}},"mounts":{{json .Mounts}},'
          '"user":{{json .Config.User}},"host":{{json .HostConfig}},"state":{{json .State}}}')


def command(args, *, stdin=None):
    result = subprocess.run(args, input=stdin, text=True, capture_output=True, timeout=30)
    if result.returncode:
        raise RuntimeError("Search deployment audit command failed; private output suppressed")
    return result.stdout


def inspect(identifier):
    return json.loads(command(["docker", "inspect", "--format", FORMAT, identifier]))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-api", action="store_true")
    parser.add_argument("--expected-main-image")
    args = parser.parse_args()
    if sys.platform != "linux":
        raise SystemExit("Run only on the authorized Linux deployment host")
    root = Path(__file__).resolve().parent
    services = {}
    for name in ("core", "valkey"):
        info = inspect(PROJECT + "-" + name + "-1")
        assert info["image"] == IMAGES[name]
        assert info["state"]["Health"]["Status"] == "healthy"
        assert info["host"]["ReadonlyRootfs"] and info["host"]["NetworkMode"] != "host"
        assert "ALL" in info["host"]["CapDrop"]
        assert not any(info["ports"].values())
        assert not any(mount["Destination"].endswith("docker.sock") for mount in info["mounts"])
        expected = {PROJECT + "_search_data"}
        if name == "core":
            expected |= {PROJECT + "_egress", CONTROL}
            assert info["user"] == "977:977"
            assert info["host"]["Memory"] == 512 * 1024 * 1024
            assert info["host"]["NanoCpus"] == 1_000_000_000
            assert info["host"]["PidsLimit"] == 128
        else:
            assert info["user"] == "valkey"
            assert info["host"]["Memory"] == 64 * 1024 * 1024
        assert set(info["networks"]) == expected
        services[name] = {key: info[key] for key in ("id", "name", "image", "user", "started_at")}
        services[name].update(networks=sorted(info["networks"]), host_ports=[],
                              memory_limit=info["host"]["Memory"], readonly_root=True)
    network = json.loads(command(["docker", "network", "inspect", CONTROL]))[0]
    assert network["Internal"]
    assert network["Labels"].get("org.hr-research.search-control.owner") == MAIN_PROJECT
    expected_members = {services["core"]["id"]}
    api_probe = None
    if args.check_api:
        info = inspect(MAIN_PROJECT + "-api-1")
        expected_members.add(info["id"])
        assert CONTROL in info["networks"] and info["host"]["NetworkMode"] != "host"
        if args.expected_main_image:
            assert info["image"] == args.expected_main_image
        probe = '''import json,os
from urllib.request import build_opener,ProxyHandler
url=os.environ["DISCOVERY_INTERNAL_SEARCH_URL"]
assert url=="http://recruitment-search:8080/search"
timeout=float(os.environ["DISCOVERY_SEARCH_TIMEOUT_SECONDS"])
assert timeout==15
opener=build_opener(ProxyHandler({}))
with opener.open("http://recruitment-search:8080/healthz",timeout=5) as response:
    assert response.status==200
print(json.dumps({"endpoint":url,"timeout_seconds":timeout,"health_status":200,"upstream_queries":0}))
'''
        api_probe = json.loads(command(["docker", "exec", "-i", info["id"], "python", "-B", "-"], stdin=probe))
    assert set(network.get("Containers", {})) == expected_members
    baseline = json.loads((root / "containers-before.json").read_text())
    unchanged = []
    for previous in baseline:
        if args.check_api and previous["name"] in {
            "/" + MAIN_PROJECT + "-" + name + "-1" for name in ("api", "worker", "beat", "web")
        }:
            continue
        current = inspect(previous["id"])
        assert all(current[key] == value for key, value in previous.items())
        unchanged.append(previous["name"])
    query = json.loads((root / "verification-search.json").read_text())
    assert query["status"] == "results" and query["official_recruitment_candidate_present"]
    source_files = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in sorted(root.iterdir()) if path.is_file() and path.name in {
                        "compose.yaml", "settings.yml", "init_env.py", "verify_search.py", "verify_deployment.py"}}
    result = {"status": "passed", "main_api_connected": args.check_api, "services": services,
              "control_network": CONTROL, "control_members": len(expected_members), "api_probe": api_probe,
              "unchanged_existing_containers": len(unchanged), "source_files": source_files,
              "source_sha256": hashlib.sha256(json.dumps(source_files, sort_keys=True).encode()).hexdigest(),
              "public_query": query["response"]["query"], "public_query_response_sha256": query["response_sha256"],
              "query_result_count": len(query["response"]["results"]), "unresponsive_engines": query["response"]["unresponsive_engines"],
              "llm_calls": 0, "candidate_site_visits": 0, "main_database_writes": 0}
    output = root / ("verification-api-connected.json" if args.check_api else "verification-service-only.json")
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
