"""The terminal chat simulator replays example scripts into golden transcripts.

The goldens in tests/golden/ are the transcripts quoted in the README, so the
docs cannot drift from what the code really does. After an intended change,
regenerate them with:  UPDATE_GOLDENS=1 pytest tests/test_cli.py
"""

from __future__ import annotations

import io
import os

import pytest

from app.cli import main
from app.config import Settings

from .conftest import REPO_ROOT

EXAMPLES = REPO_ROOT / "examples"
GOLDEN = REPO_ROOT / "tests" / "golden"
SCRIPTS = sorted(p.stem for p in EXAMPLES.glob("*.txt"))


def replay(name: str, tmp_path) -> str:
    out = io.StringIO()
    code = main(
        [
            "chat",
            "--offline",
            "--now",
            "2026-09-24T09:00",
            "--script",
            str(EXAMPLES / f"{name}.txt"),
            "--db",
            str(tmp_path / f"{name}.sqlite"),
        ],
        settings=Settings(),
        stdout=out,
    )
    assert code == 0
    return out.getvalue()


def test_there_are_example_scripts():
    assert {"booking_es", "faq_en", "manage_en", "handoff_es"} <= set(SCRIPTS)


@pytest.mark.parametrize("name", SCRIPTS)
def test_example_matches_golden_transcript(name, tmp_path):
    got = replay(name, tmp_path)
    golden = GOLDEN / f"{name}.out"
    if os.environ.get("UPDATE_GOLDENS") == "1":
        golden.parent.mkdir(exist_ok=True)
        golden.write_text(got, encoding="utf-8", newline="\n")
    assert golden.exists(), f"missing golden {golden}; run with UPDATE_GOLDENS=1"
    assert got == golden.read_text(encoding="utf-8")


def test_interactive_stdin_mode_and_db_views(tmp_path):
    db = tmp_path / "chat.sqlite"
    stdin = io.StringIO("quiero una cita\n1\n1\n1\nLuis\n1\n/state\n/quit\n")
    out = io.StringIO()
    main(["chat", "--offline", "--now", "2026-09-24T09:00", "--db", str(db)], settings=Settings(), stdin=stdin, stdout=out)
    text = out.getvalue()
    assert "¡Listo, Luis!" in text and "state=idle" in text

    views = io.StringIO()
    main(["bookings", "--db", str(db)], settings=Settings(), stdout=views)
    assert "2026-09-24 10:00  confirmed Luis" in views.getvalue()
    handoffs = io.StringIO()
    main(["handoffs", "--db", str(db)], settings=Settings(), stdout=handoffs)
    assert "(no open handoffs)" in handoffs.getvalue()


def test_forged_tap_is_rejected_in_the_simulator(tmp_path):
    stdin = io.StringIO("/tap time:03:00 03:00\n/bookings\n")
    out = io.StringIO()
    main(["chat", "--offline", "--now", "2026-09-24T09:00", "--db", str(tmp_path / "f.sqlite")], settings=Settings(), stdin=stdin, stdout=out)
    assert "Esa opción ya no está disponible" in out.getvalue()
    assert "(no bookings)" in out.getvalue()


def test_views_refuse_a_missing_database(tmp_path):
    with pytest.raises(SystemExit):
        main(["bookings", "--db", str(tmp_path / "nope.sqlite")], settings=Settings(), stdout=io.StringIO())
    assert not (tmp_path / "nope.sqlite").exists()
