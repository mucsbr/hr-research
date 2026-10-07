from copy import deepcopy

import pytest

from app.adapter_code_package import (
    build_bundle, content_sha256, has_implementation_change, load_bundle, persist_bundle,
    validate_bundle, validate_candidate_spec,
)
from app.collection import CollectionError
from app.config import get_settings


def spec(code="def run(operation, payload):\n    return payload\n", dependencies=None):
    return {"schema_version": 1, "contract": "python_adapter_v1", "kind": "boss",
            "files": {"adapter.py": code}, "dependencies": dependencies or []}


def empty_lock():
    return {"schema_version": 1, "python": "3.12", "platform": "linux/amd64", "dependencies": [],
            "wheels": [], "resolver_policy": "approved-pure-wheels-v1"}


def test_package_validation_never_executes_module_level_source(tmp_path):
    sentinel = tmp_path / "must-not-exist"
    payload = spec(f"open({str(sentinel)!r}, 'w').write('bad')\n"
                   "def run(operation, payload):\n    return payload\n")
    bundle = build_bundle(payload, empty_lock())
    assert validate_bundle(bundle, expected_sha256=content_sha256(bundle)) == bundle
    assert not sentinel.exists()


@pytest.mark.parametrize("name", ["../adapter.py", "/adapter.py", "lib/../../adapter.py", "Dockerfile",
                                  "sitecustomize.py", "requirements.pth", "lib\\bad.py"])
def test_package_rejects_escape_paths_and_execution_configuration(name):
    payload = spec()
    payload["files"][name] = "pass"
    with pytest.raises(CollectionError, match="代码包"):
        validate_candidate_spec(payload)


@pytest.mark.parametrize("dependency", ["https://other.invalid/pkg.whl", "beautifulsoup4>=4",
                                        "git+https://other.invalid/repo", "unapproved==1", "six==1 --extra-index-url=x"])
def test_package_rejects_unregistered_or_executable_dependency_inputs(dependency):
    with pytest.raises(CollectionError):
        validate_candidate_spec(spec(dependencies=[dependency]))


def test_lock_and_source_tampering_change_the_verified_package():
    bundle = build_bundle(spec(), empty_lock())
    digest = content_sha256(bundle)
    bundle["files"]["adapter.py"] += "\n# tampered\n"
    with pytest.raises(CollectionError, match="摘要"):
        validate_bundle(bundle, expected_sha256=digest)
    bundle["lock"]["wheels"] = [{"url": "https://outside.invalid/pkg.whl"}]
    with pytest.raises(CollectionError):
        validate_bundle(bundle)


def test_metadata_only_candidates_do_not_count_as_implementation_updates():
    old = spec('__version__ = "1"\n\ndef run(operation, payload):\n    "old doc"\n    return payload\n')
    new = deepcopy(old)
    new["files"]["adapter.py"] = '# reformatted\nVERSION = "2"\n\ndef run(operation, payload):\n    "new doc"\n    return payload\n'
    assert not has_implementation_change(old, new)
    new["files"]["adapter.py"] = 'def run(operation, payload):\n    return {"records": payload["items"]}\n'
    assert has_implementation_change(old, new)
    new = deepcopy(old)
    new["dependencies"] = ["soupsieve==2.7"]
    assert has_implementation_change(old, new)


def test_inert_package_storage_survives_reuse_but_rejects_tampering_and_links(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "storage_dir", tmp_path)
    bundle = build_bundle(spec(), empty_lock())
    digest = content_sha256(bundle)
    reference = persist_bundle(bundle)
    assert persist_bundle(bundle) == reference
    assert load_bundle(reference, expected_sha256=digest) == bundle
    path = tmp_path / reference
    path.write_text('{}')
    with pytest.raises(CollectionError):
        persist_bundle(bundle)
    path.unlink()
    external = tmp_path / 'external.json'
    external.write_text('{}')
    path.symlink_to(external)
    with pytest.raises(CollectionError):
        load_bundle(reference, expected_sha256=digest)
    with pytest.raises(CollectionError):
        load_bundle('../external.json', expected_sha256=digest)


@pytest.mark.parametrize("code", ["async def run(operation, payload):\n    return payload\n",
                                 "def run(payload):\n    return payload\n",
                                 "def run(operation, payload):\n    return payload\n\ud800"])
def test_invalid_entry_or_unicode_is_a_bounded_validation_error(code):
    with pytest.raises(CollectionError):
        validate_candidate_spec(spec(code))
