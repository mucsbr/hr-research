"""Finite regressions for audited receipt/state/install defects, remote Linux only."""

import json
import subprocess
import time

from common import canonical, digest
from verify_remote import Verification


def main():
    v = Verification("verification-audit-state.json")
    v.state["runtime_sha256"] = v.api("GET", "/v1/health")["runtime_sha256"]
    failed = v.prepare(
        "null-output",
        "def run(operation, payload):\n    raise ValueError('controlled null-output regression')\n",
    )
    failure = failed.get("result") or v.wait(failed)
    assert failure["status"] == "failed" and failure["receipt"]["output_sha256"] == digest(None)
    assert failure["receipt"]["execution_started"] is True
    large = v.prepare(
        "near-output-limit", "def run(operation, payload):\n    return {'text': 'x' * 3999900}\n"
    )
    result = v.wait(large)
    assert result["status"] == "succeeded" and 4_000_000 < len(canonical(result)) < 4_500_000
    assert set(result) == {"run_id", "status", "request_sha256", "output", "error_code", "receipt"}
    v.state["checks"] = ["failed_null_output_hash", "near_limit_get_bounded_without_input"]
    v.save()
    # Damage only the new QA row, and simulate a DB-commit loss for its sibling QA bundle.
    if not v.state.get("restart_injected"):
        code = """import sqlite3,sys
db=sqlite3.connect('/data/state.sqlite3')
db.execute('UPDATE runs SET state=? WHERE id=?',(b'{broken',sys.argv[1]))
db.execute('DELETE FROM bundles WHERE sha=?',(sys.argv[2],))
db.commit()
print('Only the two explicit QA records were fault-injected.')
"""
        subprocess.run(
            [
                "docker",
                "compose",
                "--env-file",
                "runner.env",
                "exec",
                "-T",
                "broker",
                "python",
                "-",
                failed["run_id"],
                large["request"]["bundle_sha256"],
            ],
            input=code,
            text=True,
            check=True,
        )
        v.state["restart_injected"] = True
        v.save()
        subprocess.run(
            ["docker", "compose", "--env-file", "runner.env", "restart", "broker"], check=True
        )
    for _ in range(60):
        try:
            if v.api("GET", "/v1/health")["status"] == "ready":
                break
        except Exception:
            pass
        time.sleep(0.2)
    preserved = v.api("GET", "/v1/runs/" + large["run_id"])
    assert preserved == result
    damaged = v.api("GET", "/v1/runs/" + failed["run_id"])
    assert damaged["status"] == "unknown" and damaged["error_code"] == "stored_state_corrupt"
    assert damaged["receipt"]["output_sha256"] == digest(None)
    duplicate = v.api("POST", "/v1/runs", failed["request"], expected=202)
    assert duplicate["run_id"] == failed["run_id"] and duplicate["status"] == "unknown"
    code = """import sqlite3,sys,json
db=sqlite3.connect('/data/state.sqlite3')
assert db.execute('SELECT 1 FROM bundles WHERE sha=?',(sys.argv[1],)).fetchone()
assert db.execute('SELECT 1 FROM quarantined_states WHERE run_id=?',(sys.argv[2],)).fetchone()
print(json.dumps({'orphan_bundle_reconciled':True,'corrupt_state_quarantined':True}))
"""
    subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            "runner.env",
            "exec",
            "-T",
            "broker",
            "python",
            "-",
            large["request"]["bundle_sha256"],
            failed["run_id"],
        ],
        input=code,
        text=True,
        check=True,
    )
    v.state["checks"] += [
        "large_result_survives_restart",
        "corrupt_row_isolated",
        "orphan_bundle_reconciled",
        "same_key_not_reexecuted",
    ]
    v.state["quarantined_result"] = damaged
    v.save()

    # A reviewed synthetic wheel exercises the kernel cap directly, not the production
    # resolver allowlist. It is never registered, published, or imported by any process.
    code = """import hashlib,json,os,shutil,uuid,zipfile
from pathlib import Path
from docker_backend import DockerBackend
root=Path('/data'); folder=root/'builds'/('audit-fs-'+uuid.uuid4().hex); folder.mkdir(parents=True)
try:
    wheel=folder/'qa_disk_probe-1.0-py3-none-any.whl'
    with zipfile.ZipFile(wheel,'w',compression=zipfile.ZIP_DEFLATED) as archive:
        with archive.open('qa_disk_probe/payload.bin','w') as stream:
            for _ in range(66): stream.write(b'x'*(1024*1024))
        archive.writestr('qa_disk_probe/__init__.py','')
        archive.writestr('qa_disk_probe-1.0.dist-info/METADATA','Metadata-Version: 2.1\\nName: qa-disk-probe\\nVersion: 1.0\\n')
        archive.writestr('qa_disk_probe-1.0.dist-info/WHEEL','Wheel-Version: 1.0\\nRoot-Is-Purelib: true\\nTag: py3-none-any\\n')
        archive.writestr('qa_disk_probe-1.0.dist-info/RECORD','')
    checksum=hashlib.sha256(wheel.read_bytes()).hexdigest()
    (folder/'requirements.lock').write_text('qa-disk-probe==1.0 --hash=sha256:'+checksum+'\\n')
    backend=DockerBackend(root,Path(os.environ['RUNNER_HOST_DATA_DIR']),os.environ['RUNNER_RUNTIME_IMAGE'],os.environ['RUNNER_SCOPE']+'-audit-fs')
    result=backend.execute(mode='install',mounts=[(folder,'/wheels',True)])
    assert result['exit_code'] != 0 and result['cleanup'], result['error_code']
    assert b'No space left on device' in result['stderr'], result['stderr'][:512]
    assert result['actual_limits']['install_tmpfs_bytes']==67108864
    safe={key:value for key,value in result.items() if key not in {'stdout','stderr'}}
    safe['enospc']=True
    Path('/data/audit-tmpfs-result.json').write_text(json.dumps(safe,indent=2))
    print(json.dumps(safe,indent=2))
finally:
    shutil.rmtree(folder)
"""
    if "tmpfs_hard_enospc" not in v.state["checks"]:
        subprocess.run(
            [
                "docker",
                "compose",
                "--env-file",
                "runner.env",
                "exec",
                "-T",
                "broker",
                "python",
                "-",
            ],
            input=code,
            text=True,
            check=True,
        )
        v.state["checks"].append("tmpfs_hard_enospc")
        v.save()
    print(
        json.dumps(
            {
                "checks": v.state["checks"],
                "runtime_sha256": v.state["runtime_sha256"],
                "llm_calls": 0,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
