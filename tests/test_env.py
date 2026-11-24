from __future__ import annotations

import os
from pathlib import Path

import pytest

from mara_coach.env import load_env

KEYS = ("MARA_TEST_A", "MARA_TEST_B", "MARA_TEST_C")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in KEYS:
        monkeypatch.delenv(key, raising=False)


def test_dotenv_lines_load_without_overriding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = tmp_path / ".env"
    env.write_text("# comment\nMARA_TEST_A='one'\nexport MARA_TEST_B=two\n")
    monkeypatch.setenv("MARA_TEST_B", "kept")
    load_env(env)
    assert os.environ["MARA_TEST_A"] == "one" and os.environ["MARA_TEST_B"] == "kept"


def test_systemd_credentials_load_one_var_per_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    creds = tmp_path / "creds"
    creds.mkdir()
    (creds / "MARA_TEST_A").write_text("sk-value\n")
    (creds / "MARA_TEST_B").write_text("from-creds\n")
    (creds / "not a name").write_text("ignored\n")
    (creds / "MARA_TEST_C").mkdir()  # not a file
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(creds))
    monkeypatch.setenv("MARA_TEST_B", "already-set")
    load_env(tmp_path / "missing.env")
    assert os.environ["MARA_TEST_A"] == "sk-value"  # trailing newline stripped
    assert os.environ["MARA_TEST_B"] == "already-set"
    assert "MARA_TEST_C" not in os.environ
    assert "not a name" not in os.environ


def test_credentials_win_over_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    creds = tmp_path / "creds"
    creds.mkdir()
    (creds / "MARA_TEST_A").write_text("from-creds")
    env = tmp_path / ".env"
    env.write_text("MARA_TEST_A=from-dotenv\nMARA_TEST_B=dotenv-only\n")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(creds))
    load_env(env)
    assert os.environ["MARA_TEST_A"] == "from-creds"
    assert os.environ["MARA_TEST_B"] == "dotenv-only"


def test_missing_credentials_directory_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path / "gone"))
    load_env(tmp_path / "missing.env")
    assert "MARA_TEST_A" not in os.environ
