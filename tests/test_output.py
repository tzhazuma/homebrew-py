from __future__ import annotations

import io

import pytest

from pybrew import output


def test_color_helpers_respect_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(output, "color_enabled", lambda: True)
    rendered = output.style("Formulae", output.BLUE, bold=True)
    assert "\033[1m" in rendered and "\033[34m" in rendered
    assert output.green("✔").startswith("\033[32m")
    assert output.yellow("Warning").startswith("\033[33m")
    assert output.red("Error").startswith("\033[31m")


def test_oh1_and_ohai_use_color_when_enabled(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(output, "color_enabled", lambda: True)
    output.oh1("Formulae")
    captured = capsys.readouterr().out
    assert "\033[1m\033[34m==>" in captured
    assert "Formulae" in captured


def test_color_is_disabled_in_non_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setattr(output.sys.stdout, "isatty", lambda: False)
    assert output.color_enabled() is False
    assert output.style("Formulae", output.BLUE, bold=True) == "Formulae"
