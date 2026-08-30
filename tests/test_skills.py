"""`groundcrew skills`: printing a bundled skill's guidance."""

from __future__ import annotations

import pytest

from groundcrew import cli


def test_skills_prints_the_named_skills_content(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.cmd_skills("finding-peers") == 0

    out = capsys.readouterr().out
    assert "name: finding-peers" in out
    assert "groundcrew sessions --json" in out
    assert "address" in out


def test_skills_reports_an_unknown_name(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.cmd_skills("no-such-skill") == 1

    assert "no such skill: no-such-skill" in capsys.readouterr().err
