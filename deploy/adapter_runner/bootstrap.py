"""Image-owned entry point. This file is never supplied by a candidate."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile

os.umask(0o077)


def main():
    if sys.argv[1:] == ["install"]:
        # No source bundle or task payload is mounted in this container.
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                "-m",
                "pip",
                "--isolated",
                "install",
                "--disable-pip-version-check",
                "--no-input",
                "--no-index",
                "--find-links=/wheels",
                "--require-hashes",
                "--only-binary=:all:",
                "--no-cache-dir",
                "--no-compile",
                "--target=/deps",
                "-r",
                "/wheels/requirements.lock",
            ],
            check=False,
            stdout=sys.stderr,
            stderr=sys.stderr,
        )
        if result.returncode:
            raise SystemExit(result.returncode)
        files = sorted(Path("/deps").rglob("*"))
        regular = [p for p in files if p.is_file()]
        if (
            len(files) > 4096
            or sum(p.stat().st_size for p in regular) > 64 * 1024 * 1024
            or any(p.is_symlink() for p in files)
        ):
            raise ValueError("dependency export limit")
        with tarfile.open(
            fileobj=sys.stdout.buffer, mode="w|", format=tarfile.USTAR_FORMAT
        ) as archive:
            for path in regular:
                info = archive.gettarinfo(str(path), arcname=str(path.relative_to("/deps")))
                if not info.isfile():
                    raise ValueError("nonregular dependency artifact")
                info.uid = info.gid = info.mtime = 0
                info.uname = info.gname = ""
                info.mode = 0o444
                with path.open("rb") as content:
                    archive.addfile(info, content)
        return
    if sys.argv[1:] != ["run"]:
        raise SystemExit(64)
    request = json.loads(Path("/input/request.json").read_text())
    # Plain sys.path insertion does not evaluate .pth or sitecustomize hooks.
    sys.path.extend(["/deps", "/bundle"])
    spec = importlib.util.spec_from_file_location("candidate_adapter", "/bundle/adapter.py")
    module = importlib.util.module_from_spec(spec)
    original_stdout = sys.stdout
    sys.stdout = sys.stderr
    try:
        spec.loader.exec_module(module)
        result = module.run(request["operation"], request["input"])
    finally:
        sys.stdout = original_stdout
    if not isinstance(result, dict):
        raise ValueError("adapter must return a JSON object")
    sys.stdout.write(json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(",", ":")))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
