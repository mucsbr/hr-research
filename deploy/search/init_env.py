"""Generate only this deployment's private SearXNG secret on authorized Linux."""

import json
import os
from pathlib import Path
import secrets
import sys


def main():
    if sys.platform != "linux":
        raise SystemExit("Generate search.env only on the authorized deployment host")
    path = Path(__file__).resolve().parent / "search.env"
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write("SEARXNG_SECRET=" + secrets.token_hex(32) + "\n")
        stream.write("DISCOVERY_SEARCH_CONTROL_NETWORK=hr-intel-verify-20261005_search_control\n")
        stream.write("DISCOVERY_SEARCH_CONTROL_SUBNET=172.16.246.0/24\n")
        stream.write("SEARCH_DATA_NETWORK_SUBNET=172.16.244.0/24\n")
        stream.write("SEARCH_EGRESS_NETWORK_SUBNET=172.16.245.0/24\n")
        stream.flush()
        os.fsync(stream.fileno())
    print(json.dumps({"private_config_created": True, "path": str(path), "secret_printed": False}))


if __name__ == "__main__":
    main()
