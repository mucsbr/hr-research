"""Remote Linux deployment helper. Never invoke Docker on the development Mac."""

import argparse
import os
from pathlib import Path
import platform
import secrets
import subprocess


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pin-runtime", action="store_true")
    args = parser.parse_args()
    if platform.system() != "Linux":
        raise SystemExit("Run this helper only on the authorized remote Linux Docker host.")
    root = Path(__file__).resolve().parent
    env = root / "runner.env"
    if not env.exists():
        socket = Path("/var/run/docker.sock")
        gid = socket.stat().st_gid
        data = root / "runner-data"
        data.mkdir(exist_ok=True)
        os.chown(data, -1, gid)
        data.chmod(0o770)
        content = {
            "RUNNER_TOKEN": secrets.token_urlsafe(48),
            "RUNNER_RUNTIME_IMAGE": "sha256:" + "0" * 64,
            "RUNNER_HOST_DATA_DIR": str(data),
            "RUNNER_DOCKER_GID": str(gid),
            "RUNNER_NETWORK_SUBNET": "172.16.242.0/24",
        }
        with env.open("x") as output:
            os.chmod(env, 0o600)
            output.write("".join(f"{key}={value}\n" for key, value in content.items()))
    if args.pin_runtime:
        image = subprocess.check_output(
            [
                "docker",
                "image",
                "inspect",
                "--format",
                "{{.Id}}",
                "hr-adapter-runtime:verify-20261005",
            ],
            text=True,
        ).strip()
        assert image.startswith("sha256:") and len(image) == 71
        lines = env.read_text().splitlines()
        env.write_text(
            "\n".join(
                "RUNNER_RUNTIME_IMAGE=" + image
                if line.startswith("RUNNER_RUNTIME_IMAGE=")
                else line
                for line in lines
            )
            + "\n"
        )
        print("Pinned runtime image:", image)
    print("Private runner.env ready; credentials were not printed.")


if __name__ == "__main__":
    main()
