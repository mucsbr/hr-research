"""Read bounded immutable upstream source snapshots, never execute their content."""
import base64
import binascii
import difflib
import hashlib
import json
from pathlib import PurePosixPath
import re
from urllib.parse import quote

from app.collection import CollectionError, PublicFetcher

FIXED_CODE_UPSTREAMS = {"boss": "eatmoreduck/boss-zhipin-scraper"}
MAX_FILES = 4
MAX_FILE_BYTES = 16000
MAX_TOTAL_BYTES = 48000
MAX_DIFF_CHARS = 10000
DEPENDENCY_NAMES = {"requirements.txt", "requirements.lock", "pyproject.toml", "uv.lock"}


def sha256(value):
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def safe_source_path(path):
    if not isinstance(path, str) or len(path) > 250 or "\\" in path or "\x00" in path:
        return False
    parsed = PurePosixPath(path)
    return (not parsed.is_absolute() and all(part not in {"", ".", ".."} for part in path.split("/"))
            and bool(re.fullmatch(r"[A-Za-z0-9_. /-]+", path))
            and (parsed.suffix == ".py" or parsed.name in DEPENDENCY_NAMES))


def _read_json(fetcher, url):
    final, body = fetcher.fetch(url)
    if final != url:
        raise CollectionError("upstream_redirect", "固定代码上游发生跳转，未扩大读取范围")
    if len(body.encode()) > 1_000_000:
        raise CollectionError("upstream_size", "上游元数据超过采样预算")
    try:
        return json.loads(body)
    except (TypeError, ValueError) as exc:
        raise CollectionError("upstream_contract_changed", "上游元数据不是有效JSON") from exc


def _read_file(fetcher, repository, commit, path):
    url = f"https://api.github.com/repos/{repository}/contents/{quote(path, safe='/')}?ref={commit}"
    payload = _read_json(fetcher, url)
    if (not isinstance(payload, dict) or payload.get("type") != "file"
            or payload.get("encoding") != "base64" or payload.get("path") != path
            or not isinstance(payload.get("size"), int) or payload["size"] > MAX_FILE_BYTES):
        raise CollectionError("upstream_file_contract", "上游文件类型、路径或大小不符合固定采样契约")
    try:
        raw = base64.b64decode("".join(payload["content"].split()), validate=True)
        content = raw.decode("utf-8")
    except (KeyError, AttributeError, binascii.Error, UnicodeError) as exc:
        raise CollectionError("upstream_file_contract", "上游源文件不能安全解码为UTF-8") from exc
    git_sha = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
    if len(raw) != payload["size"] or len(raw) > MAX_FILE_BYTES or git_sha != payload.get("sha"):
        raise CollectionError("upstream_file_digest", "上游文件字节与声明的长度或Git内容摘要不一致")
    return {"path": path, "commit": commit, "url": url, "sha256": sha256(raw), "content": content}


def sample_upstream_diff(kind, base_commit, target_commit, *, fetcher=None):
    repository = FIXED_CODE_UPSTREAMS.get(kind)
    if not repository:
        raise CollectionError("upstream_not_configured", "该平台没有登记固定代码上游")
    if any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value)
           for value in (base_commit, target_commit)):
        raise CollectionError("upstream_revision", "代码上游必须使用明确的40位提交摘要")
    if base_commit == target_commit:
        raise CollectionError("upstream_unchanged", "旧新提交相同，没有代码升级差异")
    owned = fetcher is None
    fetcher = fetcher or PublicFetcher()
    try:
        endpoint = f"https://api.github.com/repos/{repository}/compare/{base_commit}...{target_commit}"
        compare = _read_json(fetcher, endpoint)
        if (not isinstance(compare, dict) or compare.get("base_commit", {}).get("sha") != base_commit
                or compare.get("merge_base_commit", {}).get("sha") != base_commit
                or compare.get("status") != "ahead" or not isinstance(compare.get("files"), list)):
            raise CollectionError("upstream_revision_conflict", "提交比较不是登记基线的可验证前向更新")
        files = compare["files"]
        eligible = [item for item in files if isinstance(item, dict) and safe_source_path(item.get("filename"))
                    and item.get("status") in {"added", "modified", "removed", "renamed"}]
        # Include dependency evidence as well as source; never let numerous Python files hide the lock change.
        eligible.sort(key=lambda item: (PurePosixPath(item["filename"]).name not in DEPENDENCY_NAMES,
                                        item["filename"]))
        snapshots, used_bytes, diff_chars = [], 0, 0
        for item in eligible[:MAX_FILES]:
            path, status = item["filename"], item["status"]
            old_path = item.get("previous_filename", path)
            if not safe_source_path(old_path):
                raise CollectionError("upstream_path", "重命名文件的旧路径超出允许源码范围")
            before = None if status == "added" else _read_file(fetcher, repository, base_commit, old_path)
            after = None if status == "removed" else _read_file(fetcher, repository, target_commit, path)
            used_bytes += sum(len(value["content"].encode()) for value in (before, after) if value)
            if used_bytes > MAX_TOTAL_BYTES:
                raise CollectionError("upstream_size", "固定上游源码样本超过总字节预算")
            diff = "".join(difflib.unified_diff(
                (before or {}).get("content", "").splitlines(keepends=True),
                (after or {}).get("content", "").splitlines(keepends=True),
                fromfile=old_path + "@" + base_commit, tofile=path + "@" + target_commit,
            ))
            remaining = max(MAX_DIFF_CHARS - diff_chars, 0)
            sampled_diff = diff[:remaining]
            diff_chars += len(sampled_diff)
            snapshots.append({"path": path, "status": status, "before": before, "after": after,
                              "diff": sampled_diff, "diff_truncated": len(sampled_diff) != len(diff)})
        result = {"repository": "https://github.com/" + repository, "kind": kind,
                  "base_commit": base_commit, "target_commit": target_commit, "files": snapshots,
                  "sampled_files": len(snapshots), "eligible_files": len(eligible),
                  "unselected_files": max(len(eligible) - len(snapshots), 0),
                  "compare_file_limit_reached": len(files) >= 300, "source_bytes": used_bytes,
                  "coverage": "bounded_sample", "request_budget": 1 + 2 * MAX_FILES}
        result["snapshot_sha256"] = sha256(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return result
    finally:
        if owned:
            fetcher.close()
