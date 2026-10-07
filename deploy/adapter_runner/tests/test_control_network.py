"""Private configuration edits only; these tests never invoke Docker."""

from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import prepare_control_network as control


def test_private_updates_preserve_unrelated_values_and_remove_duplicate_keys(tmp_path):
    path = tmp_path / "server.env"
    path.write_text(
        "# keep comment\nUNCHANGED=a=$literal\nADAPTER_RUNNER_TOKEN=old\nADAPTER_RUNNER_TOKEN=duplicate\n"
    )
    path.chmod(0o600)
    control.update_private_env(
        path,
        {
            "ADAPTER_RUNNER_TOKEN": "fixture-only",
            "ADAPTER_RUNNER_URL": "http://adapter-runner-broker:8090",
        },
    )
    text = path.read_text()
    assert "# keep comment\nUNCHANGED=a=$literal\n" in text
    assert text.count("ADAPTER_RUNNER_TOKEN=") == 1
    assert control.private_env(path)["ADAPTER_RUNNER_TOKEN"] == "fixture-only"
    assert path.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob("*.tmp"))


def test_public_or_symlink_environment_file_is_rejected(tmp_path):
    path = tmp_path / "runner.env"
    path.write_text("RUNNER_TOKEN=fixture\n")
    path.chmod(0o644)
    with pytest.raises(RuntimeError):
        control.private_env(path)
    path.chmod(0o600)
    linked = tmp_path / "linked.env"
    linked.symlink_to(path)
    with pytest.raises(RuntimeError):
        control.private_env(linked)


def test_nonlinux_guard_fires_before_any_docker_command(tmp_path, monkeypatch):
    monkeypatch.setattr(control.sys, "platform", "darwin")
    monkeypatch.setattr(
        control.sys, "argv", ["prepare_control_network.py", "--main-dir", str(tmp_path)]
    )
    monkeypatch.setattr(
        control,
        "command",
        lambda *_args, **_kwargs: pytest.fail("Docker must not run on the local Mac"),
    )
    with pytest.raises(SystemExit, match="authorized remote Linux"):
        control.main()
