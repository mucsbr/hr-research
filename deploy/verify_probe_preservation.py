"""Compare reduced Docker metadata only; never read environment variables."""

import argparse
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--before", type=Path, required=True)
    parser.add_argument("--after", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    before = json.loads(args.before.read_text())
    after = {row["name"]: row for row in map(json.loads, args.after.read_text().splitlines())}
    changes = {f"/hr-intel-verify-20261005-{service}-1" for service in ["api", "worker", "beat", "web", "migrate"]}
    preserved = []
    for row in before:
        if row["name"].startswith("/hr-probe-") or row["name"] in changes:
            continue
        current = after[row["name"]]
        for key in ["id", "image", "started_at", "status"]:
            assert row[key] == current[key], "Unexpected change: " + row["name"] + " " + key
        assert sorted(row["networks"]) == sorted(current["networks"]), "Network membership changed"
        preserved.append(row["name"])
    result = {"status": "passed", "preserved_count": len(preserved), "preserved": preserved,
              "expected_application_replacements": sorted(changes)}
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    args.output.chmod(0o600)
    print(json.dumps({"status": "passed", "preserved_count": len(preserved)}))


if __name__ == "__main__":
    main()
