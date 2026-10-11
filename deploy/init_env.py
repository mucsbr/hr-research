"""Create independent deployment credentials without printing any secret values."""
import argparse
import base64
import os
from pathlib import Path
import re
import secrets


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="deploy/server.env")
    parser.add_argument("--project", default="hr-intelligence")
    parser.add_argument("--port", type=int, default=18080)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("port must be between 1024 and 65535")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", args.project):
        parser.error("project must start with a lowercase ASCII letter/digit and use only letters, digits, '-' or '_'")
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    values = {
        "COMPOSE_PROJECT_NAME": args.project,
        "BIND_ADDRESS": "127.0.0.1",
        "HTTP_PORT": str(args.port),
        "APP_NETWORK_SUBNET": "172.16.240.0/24",
        "DATA_NETWORK_SUBNET": "172.16.241.0/24",
        "ADAPTER_RUNNER_CONTROL_SUBNET": "172.16.243.0/24",
        "DISCOVERY_SEARCH_CONTROL_SUBNET": "172.16.246.0/24",
        "APP_URL": f"http://127.0.0.1:{args.port}",
        "COOKIE_SECURE": "false",
        "POSTGRES_PASSWORD": secrets.token_hex(32),
        "REDIS_PASSWORD": secrets.token_hex(32),
        "APP_SECRET": secrets.token_hex(48),
        "ENCRYPTION_KEY": base64.urlsafe_b64encode(os.urandom(32)).decode(),
        "ADMIN_USERNAME": "admin",
        "ADMIN_PASSWORD": secrets.token_urlsafe(32),
        "LLM_DAILY_REQUEST_LIMIT": "0",
        "LLM_BASE_URL": "",
        "LLM_API_KEY": "",
        "LLM_MODEL": "glm-5.3",
        "LLM_FALLBACK_MODEL": "deepseek-flash",
    }
    # O_EXCL refuses to overwrite credentials for existing persistent volumes.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write("".join(f"{key}={value}\n" for key, value in values.items()))
    print(f"Created {path} with permissions 0600; model requests are disabled.")


if __name__ == "__main__":
    main()
