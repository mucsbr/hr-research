"""Content-addressed adapter source packages. This module never executes source."""
import ast
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile

from app.collection import CollectionError
from app.config import get_settings

CONTRACT = "python_adapter_v1"
KINDS = {"official", "boss", "liepin", "zhaopin", "51job"}
APPROVED_DEPENDENCIES = {
    "beautifulsoup4", "soupsieve", "typing-extensions", "html5lib", "six", "webencodings", "packaging",
}
MAX_SOURCE_BYTES = 100_000
MAX_BUNDLE_BYTES = 160_000
MAX_FILES = 8


def canonical_bytes(value):
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                          allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, RecursionError, UnicodeError) as exc:
        raise CollectionError("invalid_code_package", "代码包必须为有界标准JSON") from exc


def content_sha256(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def normalized_dependencies(values):
    if not isinstance(values, list) or len(values) > 12:
        raise CollectionError("invalid_code_dependencies", "解析依赖必须为有限的精确版本列表")
    pins = {}
    for item in values:
        if not isinstance(item, str) or len(item) > 120:
            raise CollectionError("invalid_code_dependencies", "依赖必须指定批准包的精确版本")
        matched = re.fullmatch(r"([A-Za-z][A-Za-z0-9_.-]*)==([0-9][A-Za-z0-9.!+_-]{0,59})", item)
        if not matched:
            raise CollectionError("invalid_code_dependencies", "不允许URL、范围、editable或自定义安装参数")
        name = re.sub(r"[-_.]+", "-", matched[1]).lower()
        if name not in APPROVED_DEPENDENCIES or name in pins:
            raise CollectionError("invalid_code_dependencies", "依赖未登记或重复；由管理员维护允许范围")
        pins[name] = matched[2]
    return [f"{name}=={pins[name]}" for name in sorted(pins)]


def validate_candidate_spec(payload, *, kind=None):
    """Validate bytes and syntax only; no import, exec, pip or user subprocess."""
    if not isinstance(payload, dict) or set(payload) != {"schema_version", "contract", "kind", "files", "dependencies"}:
        raise CollectionError("invalid_code_package", "候选必须提供完整源码包字段，不能携带执行命令或锁哈希")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1 or payload["contract"] != CONTRACT:
        raise CollectionError("code_contract_mismatch", "不支持的解析代码接口")
    if payload["kind"] not in KINDS or (kind and payload["kind"] != kind):
        raise CollectionError("adapter_kind_mismatch", "代码包不能跨来源发布")
    files = payload["files"]
    if not isinstance(files, dict) or not 1 <= len(files) <= MAX_FILES or "adapter.py" not in files:
        raise CollectionError("invalid_code_package", "代码包必须包含adapter.py且不超过8个源码文件")
    total = 0
    for name, source in files.items():
        if (not isinstance(name, str) or not re.fullmatch(r"(?:lib/)?[A-Za-z_][A-Za-z0-9_]{0,60}\.py", name)
                or PurePosixPath(name).name in {"sitecustomize.py", "usercustomize.py", "bootstrap.py"}
                or not isinstance(source, str) or "\x00" in source):
            raise CollectionError("invalid_code_package", "代码包仅接受有限相对路径的Python文本，不接受链接或启动脚本")
        try:
            total += len(source.encode("utf-8"))
        except UnicodeError as exc:
            raise CollectionError("invalid_code_package", "代码文本必须是有效UTF-8") from exc
        if total > MAX_SOURCE_BYTES:
            raise CollectionError("code_package_budget", "候选源码超过100KB上限")
        try:
            tree = ast.parse(source, filename=name)
        except (SyntaxError, ValueError, RecursionError, MemoryError) as exc:
            raise CollectionError("invalid_code_syntax", "候选源码未通过Python语法检查") from exc
        if name == "adapter.py":
            entries = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "run"]
            if len(entries) != 1:
                raise CollectionError("code_entry_missing", "必须定义一个同步run(operation, payload)入口")
            args = entries[0].args
            if ([arg.arg for arg in [*args.posonlyargs, *args.args]] != ["operation", "payload"]
                    or args.vararg or args.kwarg or args.kwonlyargs):
                raise CollectionError("code_entry_mismatch", "代码入口参数必须为operation和payload")
    result = {**payload, "files": dict(sorted(files.items())),
              "dependencies": normalized_dependencies(payload["dependencies"])}
    if len(canonical_bytes(result)) > MAX_BUNDLE_BYTES:
        raise CollectionError("code_package_budget", "代码包JSON超过有界上限")
    return result


def validate_dependency_lock(lock, *, dependencies):
    required = {"schema_version", "python", "platform", "dependencies", "wheels", "resolver_policy"}
    if (not isinstance(lock, dict) or set(lock) != required or type(lock.get("schema_version")) is not int
            or lock["schema_version"] != 1):
        raise CollectionError("invalid_dependency_lock", "依赖锁格式不匹配")
    if (lock["resolver_policy"] != "approved-pure-wheels-v1" or lock["python"] != "3.12"
            or lock["platform"] != "linux/amd64"
            or lock["dependencies"] != normalized_dependencies(dependencies)):
        raise CollectionError("invalid_dependency_lock", "依赖锁与请求、解析策略或目标运行环境不一致")
    wheels = lock["wheels"]
    if not isinstance(wheels, list) or len(wheels) > 24:
        raise CollectionError("invalid_dependency_lock", "依赖锁必须为有限wheel列表")
    packages = {}
    for wheel in wheels:
        if not isinstance(wheel, dict) or set(wheel) != {"name", "version", "filename", "sha256"}:
            raise CollectionError("invalid_dependency_lock", "依赖锁不可携带命令或下载地址")
        if not all(isinstance(wheel[key], str) for key in wheel):
            raise CollectionError("invalid_dependency_lock", "依赖锁字段类型不正确")
        name = re.sub(r"[-_.]+", "-", wheel["name"]).lower()
        if (name not in APPROVED_DEPENDENCIES or name in packages or name != wheel["name"]
                or not re.fullmatch(r"[A-Za-z0-9_.+-]{1,200}-none-any\.whl", wheel["filename"])
                or not re.fullmatch(r"[0-9a-f]{64}", wheel["sha256"])):
            raise CollectionError("invalid_dependency_lock", "依赖wheel未登记、重复或摘要/文件名不正确")
        packages[name] = wheel["version"]
    for pin in normalized_dependencies(dependencies):
        name, version = pin.split("==", 1)
        if packages.get(name) != version:
            raise CollectionError("invalid_dependency_lock", "依赖锁缺少请求的精确版本")
    if not dependencies and wheels:
        raise CollectionError("invalid_dependency_lock", "无依赖请求不能引入额外包")
    return lock


def build_bundle(spec, lock):
    spec = validate_candidate_spec(spec)
    validate_dependency_lock(lock, dependencies=spec["dependencies"])
    result = {**spec, "lock": lock}
    if len(canonical_bytes(result)) > MAX_BUNDLE_BYTES:
        raise CollectionError("code_package_budget", "含锁代码包超过大小上限")
    return result


def validate_bundle(bundle, *, expected_sha256=None):
    if not isinstance(bundle, dict) or "lock" not in bundle:
        raise CollectionError("invalid_code_package", "发布包缺少可信依赖锁")
    result = build_bundle({key: value for key, value in bundle.items() if key != "lock"}, bundle["lock"])
    if result != bundle or (expected_sha256 and content_sha256(bundle) != expected_sha256):
        raise CollectionError("code_package_corrupt", "发布包内容与固定摘要不一致")
    return result


def has_implementation_change(old_spec, new_spec):
    old = validate_candidate_spec(old_spec)
    new = validate_candidate_spec(new_spec, kind=old["kind"])
    def semantic_files(files):
        class RemoveMetadata(ast.NodeTransformer):
            def visit_Expr(self, node):
                if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                    return None
                return self.generic_visit(node)

            def visit_Assign(self, node):
                if (isinstance(node.value, ast.Constant) and node.targets
                        and all(isinstance(target, ast.Name) and target.id in {"__version__", "VERSION"}
                                for target in node.targets)):
                    return None
                return self.generic_visit(node)

        return {name: ast.dump(RemoveMetadata().visit(ast.parse(source)), include_attributes=False)
                for name, source in files.items()}
    # This rejects obvious metadata-only changes; trusted behavior tests still decide usefulness.
    return semantic_files(old["files"]) != semantic_files(new["files"]) or old["dependencies"] != new["dependencies"]


def _bundle_path(file_ref):
    if not isinstance(file_ref, str) or not re.fullmatch(r"adapter-code/[0-9a-f]{64}\.json", file_ref):
        raise CollectionError("code_package_path", "代码包引用不属于固定存储范围")
    root = Path(get_settings().storage_dir).resolve()
    target = root / file_ref
    try:
        target.resolve().relative_to(root)
    except ValueError as exc:
        raise CollectionError("code_package_path", "代码包路径不能指向存储范围之外") from exc
    if target.is_symlink():
        raise CollectionError("code_package_path", "代码包不接受文件链接")
    return target


def persist_bundle(bundle):
    """Store source as inert JSON, not as importable Python files on this host."""
    validate_bundle(bundle)
    digest = content_sha256(bundle)
    file_ref = f"adapter-code/{digest}.json"
    path = _bundle_path(file_ref)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = _bundle_path(file_ref)
    if path.exists():
        load_bundle(file_ref, expected_sha256=digest)
        return file_ref
    descriptor, temporary = tempfile.mkstemp(prefix=f".{digest}-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(canonical_bytes(bundle))
            handle.flush()
            os.fsync(handle.fileno())
        try:
            # Link a fully written inode atomically without replacing an existing immutable artifact.
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError:
            load_bundle(file_ref, expected_sha256=digest)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return file_ref


def load_bundle(file_ref, *, expected_sha256):
    if not isinstance(expected_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise CollectionError("code_package_corrupt", "代码包缺少可信摘要")
    if file_ref != f"adapter-code/{expected_sha256}.json":
        raise CollectionError("code_package_corrupt", "代码包引用与版本摘要不同")
    path = _bundle_path(file_ref)
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BUNDLE_BYTES:
                raise CollectionError("code_package_corrupt", "代码包不是有界普通文件")
            raw = handle.read(MAX_BUNDLE_BYTES + 1)
        if len(raw) > MAX_BUNDLE_BYTES or hashlib.sha256(raw).hexdigest() != expected_sha256:
            raise CollectionError("code_package_corrupt", "代码包文件已损坏或变化")
        bundle = json.loads(raw)
    except (OSError, ValueError, UnicodeError, RecursionError) as exc:
        raise CollectionError("code_package_unavailable", "固定代码包文件不可用，不从未知位置读取") from exc
    return validate_bundle(bundle, expected_sha256=expected_sha256)
