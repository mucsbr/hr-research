"""Package a cross-built .app while preserving framework symlinks and executable modes.

Runs only in the remote packaging environment; it is not a native macOS validation.
"""
import argparse
import os
from pathlib import Path
import stat
import zipfile


def pack(application, output):
    if not application.is_dir() or application.suffix != ".app":
        raise ValueError("Expected an actual .app bundle")
    temporary = output.with_suffix(output.suffix + ".part")
    with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as archive:
        for directory, dirs, files in os.walk(application, followlinks=False):
            paths = [Path(directory) / name for name in dirs + files]
            for path in paths:
                metadata = path.lstat()
                name = str(path.relative_to(application.parent))
                info = zipfile.ZipInfo(name + "/" if stat.S_ISDIR(metadata.st_mode) else name)
                info.create_system = 3
                info.external_attr = metadata.st_mode << 16
                if path.is_symlink():
                    archive.writestr(info, os.readlink(path).encode())
                elif path.is_file():
                    archive.write(path, name)
                elif path.is_dir():
                    archive.writestr(info, b"")
    temporary.replace(output)
    with zipfile.ZipFile(output) as archive:
        if archive.testzip() is not None:
            raise RuntimeError("macOS archive failed CRC verification")
        if not any(stat.S_ISLNK(item.external_attr >> 16) for item in archive.infolist()):
            raise RuntimeError("Framework symlinks were not preserved")
    return output.stat().st_size


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--application", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print({"archive_bytes": pack(args.application, args.output), "symlinks_preserved": True,
           "native_macos_validation": False})


if __name__ == "__main__":
    main()
