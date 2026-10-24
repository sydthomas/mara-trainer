from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mara_coach import profile
from mara_coach.cli import app
from mara_coach.profile import ProfileError

runner = CliRunner()

EMPTY = {
    "diet": None,
    "allergies": None,
    "shoe_likes": None,
    "shoe_dislikes": None,
    "current_shoes": None,
    "injury_history": None,
    "notes": None,
}


def test_empty_profile_is_all_null(conn: sqlite3.Connection) -> None:
    got = profile.get_profile(conn)
    assert {k: got[k] for k in EMPTY} == EMPTY
    assert got["updated_at"] is None


def test_partial_updates_only_change_given_fields(conn: sqlite3.Connection) -> None:
    profile.update_profile(conn, {"diet": "Vegetarian", "shoe_likes": ["Shoe A", "Shoe B"]})
    profile.update_profile(conn, {"allergies": ["peanuts"]})
    got = profile.get_profile(conn)
    assert got["diet"] == "Vegetarian"
    assert got["shoe_likes"] == ["Shoe A", "Shoe B"]
    assert got["allergies"] == ["peanuts"]
    assert got["shoe_dislikes"] is None and got["injury_history"] is None
    assert got["updated_at"]


def test_none_clears_a_field_and_empty_list_means_none_reported(
    conn: sqlite3.Connection,
) -> None:
    profile.update_profile(conn, {"diet": "Vegan", "allergies": []})
    assert profile.get_profile(conn)["allergies"] == []
    profile.update_profile(conn, {"diet": None})
    got = profile.get_profile(conn)
    assert got["diet"] is None and got["allergies"] == []


def test_text_is_stripped_and_list_items_deduped(conn: sqlite3.Connection) -> None:
    profile.update_profile(
        conn, {"notes": "  likes early runs ", "current_shoes": ["X", " X ", "Y"]}
    )
    got = profile.get_profile(conn)
    assert got["notes"] == "likes early runs"
    assert got["current_shoes"] == ["X", "Y"]


@pytest.mark.parametrize(
    ("updates", "fragment"),
    [
        ({"weight": "x"}, "unknown"),
        ({"diet": 3}, "diet"),
        ({"diet": "x" * 2001}, "diet"),
        ({"allergies": "peanuts"}, "allergies"),
        ({"allergies": ["ok", ""]}, "allergies"),
        ({"shoe_likes": ["x" * 101]}, "shoe_likes"),
        ({"shoe_dislikes": [f"s{i}" for i in range(31)]}, "shoe_dislikes"),
        ({}, "at least one"),
    ],
)
def test_rejects_bad_updates(
    conn: sqlite3.Connection, updates: dict[str, object], fragment: str
) -> None:
    with pytest.raises(ProfileError) as exc:
        profile.update_profile(conn, updates)
    assert any(fragment in p for p in exc.value.problems)
    assert profile.get_profile(conn)["updated_at"] is None


def test_db_rejects_non_array_lists(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO athlete_profile(id, allergies) VALUES (1, '\"peanuts\"')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO athlete_profile(id) VALUES (2)")


def _invoke(*args: str):  # type: ignore[no-untyped-def]
    return runner.invoke(app, list(args))


def test_cli_profile_set_and_show(tmp_db: Path) -> None:
    r = _invoke("profile", "show")
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["profile"]["diet"] is None
    r = _invoke(
        "profile",
        "set",
        "--diet",
        "Pescatarian",
        "--allergies",
        "tree nuts, shellfish",
        "--shoe-likes",
        "Shoe A",
        "--injury-history",
        "Synthetic note: old ankle sprain.",
    )
    assert r.exit_code == 0, r.output
    got = json.loads(r.output)["profile"]
    assert got["allergies"] == ["tree nuts", "shellfish"]
    r = _invoke("profile", "set", "--notes", "Prefers mornings", "--clear", "diet")
    assert r.exit_code == 0, r.output
    got = json.loads(_invoke("profile", "show").output)["profile"]
    assert got["diet"] is None and got["notes"] == "Prefers mornings"
    assert got["shoe_likes"] == ["Shoe A"]


def test_cli_profile_set_empty_list_and_errors(tmp_db: Path) -> None:
    r = _invoke("profile", "set", "--allergies", "")
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["profile"]["allergies"] == []
    assert _invoke("profile", "set").exit_code == 1
    assert _invoke("profile", "set", "--clear", "weight").exit_code == 1
    assert _invoke("profile", "set", "--diet", "x", "--clear", "diet").exit_code == 1
