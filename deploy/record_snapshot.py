"""Record non-secret build inputs so concurrent development cannot blur validation scope."""
import argparse
import hashlib
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="deploy/verification-results-source.json")
    args = parser.parse_args()
    paths = []
    for folder in ["backend/app", "backend/migrations", "frontend/src", "adapters"]:
        paths.extend(path for path in Path(folder).rglob("*") if path.is_file()
                     and "__pycache__" not in path.parts and path.suffix not in {".pyc", ".log"})
    paths.extend(Path(name) for name in [
        "compose.yaml", ".dockerignore", "backend/Dockerfile", "backend/pyproject.toml",
        "backend/requirements.lock", "backend/alembic.ini", "frontend/Dockerfile",
        "frontend/package.json", "frontend/package-lock.json", "frontend/index.html",
        "frontend/vite.config.ts", "frontend/tsconfig.json", "deploy/nginx.conf",
    ])
    files = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(paths)}
    digest = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    result = {"files": files, "sha256": digest}
    Path(args.output).write_text(json.dumps(result, indent=2))
    print(json.dumps({"files": len(files), "sha256": digest, "output": args.output}))


if __name__ == "__main__":
    main()
