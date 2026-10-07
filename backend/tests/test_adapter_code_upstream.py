import base64
import hashlib
import json

import pytest

from app.adapter_code_upstream import sample_upstream_diff, safe_source_path
from app.collection import CollectionError

OLD, NEW = "a" * 40, "b" * 40
ROOT = "https://api.github.com/repos/eatmoreduck/boss-zhipin-scraper"


class SnapshotFetcher:
    def __init__(self, files=None):
        self.requests = []
        self.files = files or [{"filename": "parser.py", "status": "modified"}]
        self.redirect = False
        self.bad_hash = False

    def fetch(self, url):
        self.requests.append(url)
        if "/compare/" in url:
            payload = {"base_commit": {"sha": OLD}, "merge_base_commit": {"sha": OLD},
                       "status": "ahead", "files": self.files}
        else:
            path = url.split("/contents/", 1)[1].split("?", 1)[0]
            raw = ("new = 2\n" if url.endswith(NEW) else "old = 1\n").encode()
            digest = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
            payload = {"type": "file", "encoding": "base64", "size": len(raw), "path": path,
                       "sha": "bad" if self.bad_hash else digest, "content": base64.b64encode(raw).decode()}
        return (url + "redirect" if self.redirect else url), json.dumps(payload)


def test_fixed_sha_real_source_diff_is_bounded_and_hashed():
    fetcher = SnapshotFetcher()
    snapshot = sample_upstream_diff("boss", OLD, NEW, fetcher=fetcher)
    assert len(fetcher.requests) == 3
    assert snapshot["sampled_files"] == 1 and snapshot["coverage"] == "bounded_sample"
    assert "-old = 1" in snapshot["files"][0]["diff"]
    assert "+new = 2" in snapshot["files"][0]["diff"]
    assert len(snapshot["snapshot_sha256"]) == 64
    assert all(url.startswith(ROOT) for url in fetcher.requests)


def test_dependency_diff_is_not_hidden_by_many_python_files():
    fetcher = SnapshotFetcher([{"filename": f"parser{i}.py", "status": "modified"} for i in range(8)]
                              + [{"filename": "requirements.txt", "status": "modified"}])
    snapshot = sample_upstream_diff("boss", OLD, NEW, fetcher=fetcher)
    assert snapshot["sampled_files"] == 4 and snapshot["unselected_files"] == 5
    assert snapshot["files"][0]["path"] == "requirements.txt"
    assert len(fetcher.requests) == 9


@pytest.mark.parametrize("path", ["../adapter.py", "/adapter.py", "lib/../adapter.py", "x\\adapter.py", "README.md", "setup.sh"])
def test_only_safe_source_and_dependency_paths(path):
    assert not safe_source_path(path)


def test_docs_only_diff_has_no_source_read():
    fetcher = SnapshotFetcher([{"filename": "README.md", "status": "modified"}])
    snapshot = sample_upstream_diff("boss", OLD, NEW, fetcher=fetcher)
    assert snapshot["files"] == [] and len(fetcher.requests) == 1


def test_redirect_and_bad_blob_digest_fail_closed():
    fetcher = SnapshotFetcher()
    fetcher.redirect = True
    with pytest.raises(CollectionError, match="跳转"):
        sample_upstream_diff("boss", OLD, NEW, fetcher=fetcher)
    fetcher.redirect, fetcher.bad_hash = False, True
    with pytest.raises(CollectionError) as failed:
        sample_upstream_diff("boss", OLD, NEW, fetcher=fetcher)
    assert failed.value.code == "upstream_file_digest"


def test_unknown_repo_revision_and_equal_commit_do_not_fetch():
    fetcher = SnapshotFetcher()
    for kind, before, after in [("liepin", OLD, NEW), ("boss", "main", NEW), ("boss", OLD, OLD)]:
        with pytest.raises(CollectionError):
            sample_upstream_diff(kind, before, after, fetcher=fetcher)
    assert fetcher.requests == []


def test_renamed_old_path_cannot_escape_source_scope():
    fetcher = SnapshotFetcher([{"filename": "parser.py", "previous_filename": "../../secret.py", "status": "renamed"}])
    with pytest.raises(CollectionError) as failed:
        sample_upstream_diff("boss", OLD, NEW, fetcher=fetcher)
    assert failed.value.code == "upstream_path" and len(fetcher.requests) == 1
