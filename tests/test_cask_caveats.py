"""Tests for cask caveats rendering (literal and captured Ruby block)."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from pybrew.cask import parse_cask_source
from pybrew.caveats import CaveatsError, run_cask_caveats
from pybrew.paths import Config

_RUBY = os.environ.get("PYBREW_RUBY") or "/usr/bin/ruby"
requires_ruby = pytest.mark.skipif(
    shutil.which(_RUBY) is None and not Path(_RUBY).is_file(),
    reason="Ruby interpreter not installed",
)


def _config(tmp_path: Path) -> Config:
    return Config(prefix=tmp_path / "prefix", cache=tmp_path / "cache")


def test_cask_parser_captures_caveats_block() -> None:
    cask = parse_cask_source(
        '''cask "caveaty" do
  version "1.0"
  caveats do
    <<~EOS
      token=#{token} version=#{version}
    EOS
  end
  app "Caveaty.app"
end
'''
    )
    assert cask.caveats_source is not None
    assert "token=" in cask.caveats_source
    assert cask.caveats is None


def test_cask_literal_caveats_is_returned_without_ruby(tmp_path: Path) -> None:
    cask = parse_cask_source(
        'cask "literal" do\n  version "1.0"\n  caveats "plain text"\nend\n'
    )
    assert run_cask_caveats(cask, _config(tmp_path)) == "plain text"


@requires_ruby
def test_cask_caveats_block_uses_token_version_appdir(tmp_path: Path) -> None:
    cask = parse_cask_source(
        '''cask "caveaty" do
  version "2.3"
  caveats do
    <<~EOS
      token=#{token}
      version=#{version.major_minor}
      appdir=#{appdir}
    EOS
  end
  app "Caveaty.app"
end
'''
    )
    config = _config(tmp_path)
    output = run_cask_caveats(cask, config)
    assert "token=caveaty\n" in output
    assert "version=2.3\n" in output
    assert f"appdir={config.applications}\n" in output


@requires_ruby
def test_cask_caveats_platform_block(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cask = parse_cask_source(
        '''cask "platformy" do
  version "1.0"
  caveats do
    on_macos do
      "macos"
    end
    on_linux do
      "linux"
    end
  end
end
'''
    )
    monkeypatch.setenv("PYBREW_TARGET_OS", "linux")
    assert run_cask_caveats(cask, _config(tmp_path)).strip() == "linux"


def test_caveats_cli_cask(run_cli, sample) -> None:
    cask_path = sample.tap / "Casks" / "c" / "caveaty.rb"
    cask_path.parent.mkdir(parents=True)
    cask_path.write_text(
        'cask "caveaty" do\n  version "1.0"\n  caveats "cli caveat"\nend\n'
    )
    proc = run_cli("caveats", "--cask", "caveaty")
    assert proc.returncode == 0, proc.stderr
    assert "cli caveat" in proc.stdout


def test_caveats_cli_missing_cask_returns_error(run_cli) -> None:
    proc = run_cli("caveats", "--cask", "does-not-exist")
    assert proc.returncode == 1
    assert "Error:" in proc.stderr
