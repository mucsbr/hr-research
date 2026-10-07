#!/bin/sh
# Explicit source allowlist: never upload local secrets, databases or login profiles.
set -eu
if [ "$#" -ne 2 ]; then
  echo 'Usage: scripts/remote_sync.sh user@host relative-remote-directory' >&2
  exit 2
fi
case "$2" in
  ''|/*|*..*|*[!A-Za-z0-9_/-]*) echo 'Use a simple relative remote directory.' >&2; exit 2 ;;
esac
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$root"
rsync -az --chmod=Du=rwx,Dgo=,Fu=rw,Fgo= \
  -e 'ssh -o BatchMode=yes -o ConnectTimeout=10' \
  --exclude='node_modules/' --exclude='dist/' --exclude='__pycache__/' \
  --exclude='.pytest_cache/' --exclude='*.pyc' --exclude='*.tsbuildinfo' \
  --exclude='.env' --exclude='.env.*' --exclude='*.env' --exclude='*.env.*' \
  --exclude='.runtime/' --exclude='.git/' --exclude='*.log' --exclude='*.pid' \
  --exclude='verification-results*' --exclude='backups/' \
  compose.yaml compose.bettafish.yaml compose.code-maintenance.yaml compose.search.yaml .dockerignore backend frontend adapters deploy scripts \
  "$1:$2/"
echo 'Uploaded allowed project sources only; no remote files were deleted.'
