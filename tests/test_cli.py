from __future__ import annotations

import hashlib
import io
import json
import tarfile

from pathlib import Path


def test_info_json(run_cli) -> None:
    proc = run_cli("info", "--json", "hello")
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    formula = payload["formulae"][0]
    assert formula["name"] == "hello"
    assert formula["versions"]["stable"] == "1.0"
    assert formula["desc"] == "Test formula for hello"
    assert formula["license"] == "MIT"
    assert formula["bottle"]["stable"]["files"]


def test_info_human(run_cli) -> None:
    proc = run_cli("info", "hello")
    assert proc.returncode == 0, proc.stderr
    assert "hello: stable 1.0" in proc.stdout
    assert "Not installed" in proc.stdout
    assert "Bottles:" in proc.stdout


def test_search(run_cli) -> None:
    proc = run_cli("search", "hello")
    assert proc.returncode == 0
    assert "hello:" in proc.stdout
    assert "hello-dep" in proc.stdout


def test_deps_tree(run_cli) -> None:
    proc = run_cli("deps", "--tree", "hello-dep")
    assert proc.returncode == 0, proc.stderr
    assert "hello-dep" in proc.stdout
    assert "dep" in proc.stdout


def test_list_and_config(run_cli, sample) -> None:
    assert run_cli("install", "hello", bottle_tag=sample.tag).returncode == 0
    listed = run_cli("list", "--versions")
    assert listed.returncode == 0
    assert "hello 1.0" in listed.stdout

    config = run_cli("config", "--json")
    assert config.returncode == 0
    payload = json.loads(config.stdout)
    assert payload["prefix"] == str(sample.prefix)
    assert payload["cellar"] == str(sample.prefix / "Cellar")
    assert payload["pybrew_version"].startswith("pybrew ")


def test_doctor_after_install(run_cli, sample) -> None:
    assert run_cli("install", "hello", bottle_tag=sample.tag).returncode == 0
    proc = run_cli("doctor")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "healthy" in proc.stdout


def test_parse_command(run_cli) -> None:
    corpus = Path(__file__).parent / "fixtures" / "core_formulas" / "hello.rb"
    proc = run_cli("parse", str(corpus), "--json")
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["name"] == "hello"
    assert payload["versions"]["stable"] == "2.12.3"


def test_unknown_formula_errors(run_cli) -> None:
    proc = run_cli("info", "definitely-not-a-formula-xyz")
    assert proc.returncode == 1
    assert "no formula named" in proc.stderr.lower()


def test_doctor_json_and_missing_cask_artifact(run_cli, sample) -> None:
    assert run_cli("install", "hello", bottle_tag=sample.tag).returncode == 0
    healthy = run_cli("doctor", "--json")
    assert healthy.returncode == 0, healthy.stdout + healthy.stderr
    payload = json.loads(healthy.stdout)
    assert payload["ok"] is True and payload["issues"] == []

    assert run_cli("cask", "install", "testapp").returncode == 0
    app = sample.prefix / "Applications" / "Test.app"
    import shutil

    shutil.rmtree(app)
    broken = run_cli("doctor", "--json")
    assert broken.returncode == 1
    payload = json.loads(broken.stdout)
    assert payload["ok"] is False
    assert any("missing cask artifact" in issue["message"] for issue in payload["issues"])


def test_shellenv(run_cli, sample) -> None:
    proc = run_cli("shellenv")
    assert proc.returncode == 0
    assert f'export HOMEBREW_PREFIX="{sample.prefix}"' in proc.stdout
    fish = run_cli("shellenv", "--shell", "fish")
    assert fish.returncode == 0
    assert "set -gx HOMEBREW_PREFIX" in fish.stdout


def test_cask_flags_on_search_outdated_upgrade(run_cli, sample) -> None:
    assert run_cli("install", "--cask", "testapp").returncode == 0
    search = run_cli("search", "--cask", "test")
    assert search.returncode == 0, search.stderr
    assert "testapp" in search.stdout
    cask_file = sample.tap / "Casks" / "t" / "testapp.rb"
    cask_file.write_text(cask_file.read_text().replace('version "1.0"', 'version "2.0"'))
    outdated = run_cli("outdated", "--cask")
    assert outdated.returncode == 0, outdated.stderr
    assert "testapp 1.0 < 2.0" in outdated.stdout
    payload = json.loads(run_cli("outdated", "--cask", "--json").stdout)
    assert payload["casks"][0]["current"] == "2.0"
    upgraded = run_cli("upgrade", "--cask", "testapp")
    assert upgraded.returncode == 0, upgraded.stderr
    assert "Upgraded: testapp 2.0" in upgraded.stdout


def test_fetch_formula_and_cask(run_cli, sample) -> None:
    proc = run_cli("fetch", "hello", bottle_tag=sample.tag)
    assert proc.returncode == 0, proc.stderr
    bottle_path = Path(proc.stdout.strip())
    assert bottle_path.is_file()
    assert not (sample.prefix / "Cellar" / "hello").exists()

    cask = run_cli("fetch", "--cask", "testapp")
    assert cask.returncode == 0, cask.stderr
    cask_path = Path(cask.stdout.strip())
    assert cask_path.is_file()
    assert not (sample.prefix / "Caskroom").exists()


def test_cat_home_edit_and_cask(run_cli, sample) -> None:
    cat = run_cli("cat", "hello")
    assert cat.returncode == 0, cat.stderr
    assert "class Hello" in cat.stdout
    cask_cat = run_cli("cat", "--cask", "testapp")
    assert cask_cat.returncode == 0
    assert 'cask "testapp"' in cask_cat.stdout

    home = run_cli("home", "hello")
    assert home.returncode == 0
    assert "https://example.com/hello" in home.stdout
    cask_home = run_cli("home", "--cask", "testapp")
    assert cask_home.returncode == 0
    assert "https://example.com/testapp" in cask_home.stdout

    edit = run_cli("edit", "hello", extra_env={"EDITOR": "true"})
    assert edit.returncode == 0, edit.stderr


def test_linkage_command_after_install(run_cli, sample) -> None:
    assert run_cli("install", "hello", bottle_tag=sample.tag).returncode == 0
    proc = run_cli("linkage", "hello")
    assert proc.returncode == 0, proc.stderr
    assert "linkage OK" in proc.stdout

    proc = run_cli("linkage", "hello", "--json")
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["formula"] == "hello"
    assert payload["ok"] is True


def test_livecheck_command_offline_page(run_cli, tmp_path: Path) -> None:
    page = tmp_path / "page.html"
    page.write_text("Livecheck 1.0", encoding="utf-8")
    proc = run_cli("livecheck", "livecheck", "--page", str(page), "--json", "--ruby-cask")
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["status"] == "up_to_date"
    assert payload["current_version"] == "1.0"

    page.write_text("Livecheck 2.0", encoding="utf-8")
    proc = run_cli("livecheck", "livecheck", "--page", str(page), "--json", "--ruby-cask")
    assert proc.returncode == 1, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["status"] == "outdated"
    assert payload["latest_version"] == "2.0"


def test_linkage_all_after_install(run_cli, sample) -> None:
    assert run_cli("install", "hello", bottle_tag=sample.tag).returncode == 0
    proc = run_cli("linkage", "--all", "--json")
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["formula"] == "all installed kegs"
    assert payload["ok"] is True


def test_livecheck_headers_and_invalid_header(run_cli, tmp_path: Path) -> None:
    page = tmp_path / "page.html"
    page.write_text("Livecheck 1.0", encoding="utf-8")
    proc = run_cli(
        "livecheck", "livecheck", "--page", str(page), "--header", "X-Test: 1", "--ruby-cask"
    )
    assert proc.returncode == 0, proc.stderr
    assert "up_to_date" in proc.stdout
    proc = run_cli("livecheck", "livecheck", "--page", str(page), "--header", "bad")
    assert proc.returncode == 1
    assert "invalid --header" in proc.stderr


def test_linkage_json_structure(run_cli, sample) -> None:
    assert run_cli("install", "hello", bottle_tag=sample.tag).returncode == 0
    proc = run_cli("linkage", "hello", "--json")
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert isinstance(payload["checked"], int)
    assert payload["issues"] == []
    assert isinstance(payload["unsupported"], list)
    assert isinstance(payload["corrupt"], list)
    assert isinstance(payload["audit"], list)


def test_doctor_linkage_json(run_cli, sample) -> None:
    assert run_cli("install", "hello", bottle_tag=sample.tag).returncode == 0
    proc = run_cli("doctor", "--linkage", "--json")
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["ok"] is True
    assert payload["linkage"]["issues"] == []
    assert isinstance(payload["linkage"]["checked"], int)


def test_search_falls_back_to_homebrew_formula_index(run_cli, tmp_path: Path) -> None:
    index = tmp_path / "formula.json"
    index.write_text(
        '[{"name":"remote-only","desc":"Remote fixture","aliases":["remote-alias"],'
        '"versions":{"stable":"1.2"},"homepage":"https://example.com"}]'
    )
    proc = run_cli(
        "search",
        "remote-only",
        extra_env={"PYBREW_FORMULA_SEARCH_API": index.as_uri()},
    )
    assert proc.returncode == 0, proc.stderr
    assert "remote-only: Remote fixture" in proc.stdout


def test_search_falls_back_to_homebrew_cask_index(run_cli, tmp_path: Path) -> None:
    index = tmp_path / "cask.json"
    index.write_text(
        '[{"token":"remote-cask","name":["Remote Cask"],"desc":"Cask fixture",'
        '"version":"2.0"}]'
    )
    proc = run_cli(
        "search",
        "--cask",
        "remote-cask",
        extra_env={"PYBREW_CASK_SEARCH_API": index.as_uri()},
    )
    assert proc.returncode == 0, proc.stderr
    assert "remote-cask: Remote Cask Cask fixture" in proc.stdout


def test_online_search_index_is_cached(tmp_path: Path) -> None:
    from pybrew.api import search_formulae_online

    index = tmp_path / "formula.json"
    index.write_text('[{"name":"cached-remote","desc":"Cached"}]')
    cache = tmp_path / "cache" / "formula.json"
    first = search_formulae_online("cached", api_url=index.as_uri(), cache_path=cache)
    assert [formula.name for formula in first] == ["cached-remote"]
    assert cache.is_file()
    index.unlink()
    second = search_formulae_online("cached", api_url=index.as_uri(), cache_path=cache)
    assert [formula.name for formula in second] == ["cached-remote"]


def test_doctor_reports_formula_deleted_from_tap(run_cli, sample) -> None:
    assert run_cli("install", "hello", bottle_tag=sample.tag).returncode == 0
    (sample.tap / "Formula" / "hello.rb").unlink()
    proc = run_cli("doctor")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "no longer in its tap" in proc.stdout


def test_cli_services_cleanup_keeps_custom_installed_labels(
    run_cli, sample
) -> None:
    text = sample.formula.read_text(encoding="utf-8")
    service_block = (
        "  service do\n"
        '    run [opt_bin/"hello"]\n'
        '    name macos: "com.example.hello", linux: "custom-hello"\n'
        "  end\n"
    )
    sample.formula.write_text(
        text.rsplit("end", 1)[0] + service_block + "end\n", encoding="utf-8"
    )
    install = run_cli("install", "hello", bottle_tag=sample.tag)
    assert install.returncode == 0, install.stderr + install.stdout

    agents = sample.root / "service-agents"
    agents.mkdir(parents=True, exist_ok=True)
    kept = [
        agents / "com.example.hello.plist",
        agents / "sh.brew.hello.plist",
    ]
    stale = [
        agents / "com.example.ghost.plist",
        agents / "homebrew.ghost.service",
    ]
    for path in [*kept, *stale]:
        path.write_text("service", encoding="utf-8")

    proc = run_cli("services", "cleanup")

    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert all(path.exists() for path in kept)
    assert all(not path.exists() for path in stale)
    assert "com.example.ghost.plist" in proc.stdout


def _write_option_cli_fixture(sample, tmp_path: Path) -> Path:
    project = tmp_path / "optcli-src"
    project.mkdir()
    for marker in ("ssl", "no-ssl", "readline-off", "bottle"):
        (project / f"{marker}.txt").write_text(f"{marker}\n", encoding="utf-8")
    archive = tmp_path / "src" / "optcli-1.0.tar.gz"
    archive.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "w:gz") as tar:
        for path in sorted(project.iterdir()):
            data = path.read_bytes()
            info = tarfile.TarInfo(f"optcli-1.0/{path.name}")
            info.size = len(data)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
    formula = sample.tap / "Formula" / "optcli.rb"
    formula.write_text(
        "class Optcli < Formula\n"
        '  desc "CLI option fixture"\n'
        '  homepage "https://example.com/optcli"\n'
        f'  url "{archive.as_uri()}"\n'
        f'  sha256 "{hashlib.sha256(archive.read_bytes()).hexdigest()}"\n'
        '  license "MIT"\n'
        '  option "with-ssl", "enable ssl"\n'
        '  option "without-readline", "disable readline"\n'
        "\n  def install\n"
        '    if build.with?("ssl")\n'
        '      bin.install "ssl.txt"\n'
        "    else\n"
        '      bin.install "no-ssl.txt"\n'
        "    end\n"
        '    if build.without?("readline")\n'
        '      bin.install "readline-off.txt"\n'
        "    end\n"
        '    if build.bottle?\n'
        '      bin.install "bottle.txt"\n'
        "    end\n"
        "  end\n"
        "end\n",
        encoding="utf-8",
    )
    return formula


def test_install_formula_options_from_cli_and_env(run_cli, sample, tmp_path: Path) -> None:
    _write_option_cli_fixture(sample, tmp_path)
    proc = run_cli(
        "install",
        "--build-from-source",
        "--with-ssl",
        "--without-readline",
        "--build-bottle",
        "optcli",
    )
    assert proc.returncode == 0, proc.stderr
    bin_dir = sample.prefix / "Cellar" / "optcli" / "1.0" / "bin"
    assert (bin_dir / "ssl.txt").is_file()
    assert not (bin_dir / "no-ssl.txt").exists()
    assert (bin_dir / "readline-off.txt").is_file()
    assert (bin_dir / "bottle.txt").is_file()

    env_prefix = tmp_path / "env-prefix"
    env_proc = run_cli(
        "install",
        "--build-from-source",
        "optcli",
        prefix=env_prefix,
        extra_env={
            "PYBREW_BUILD_OPTIONS": "with-ssl,without-readline,build-bottle"
        },
    )
    assert env_proc.returncode == 0, env_proc.stderr
    env_bin = env_prefix / "Cellar" / "optcli" / "1.0" / "bin"
    assert (env_bin / "ssl.txt").is_file()
    assert (env_bin / "readline-off.txt").is_file()
    assert (env_bin / "bottle.txt").is_file()


def test_formula_options_are_install_only(run_cli) -> None:
    proc = run_cli("info", "--with-ssl", "hello")
    assert proc.returncode == 2
    assert "unrecognized arguments" in proc.stderr


def test_homebrew_style_install_output(run_cli, sample) -> None:
    proc = run_cli("install", "hello", bottle_tag=sample.tag)
    assert proc.returncode == 0, proc.stderr
    assert "==> Pouring hello 1.0" in proc.stdout
    assert "🍺" in proc.stdout
    assert "files," in proc.stdout


def test_homebrew_command_aliases(run_cli, sample) -> None:
    assert run_cli("install", "hello", bottle_tag=sample.tag).returncode == 0
    listed = run_cli("ls")
    assert listed.returncode == 0, listed.stderr
    assert "hello" in listed.stdout
    info = run_cli("abv", "hello")
    assert info.returncode == 0, info.stderr
    assert "hello: stable 1.0" in info.stdout
    searched = run_cli("-S", "hello")
    assert searched.returncode == 0, searched.stderr
    assert "hello" in searched.stdout


def test_info_reports_installed_dependents(run_cli, sample) -> None:
    assert run_cli("install", "hello-dep", bottle_tag=sample.tag).returncode == 0
    proc = run_cli("info", "dep")
    assert proc.returncode == 0, proc.stderr
    assert "Dependents: hello-dep" in proc.stdout


def test_missing_reports_absent_runtime_dependency(run_cli, sample) -> None:
    assert run_cli("install", "hello-dep", bottle_tag=sample.tag).returncode == 0
    # Remove only the dependency's keg, leaving hello-dep's receipt behind.
    dep = sample.prefix / "Cellar" / "dep"
    import shutil
    shutil.rmtree(dep)
    proc = run_cli("missing")
    assert proc.returncode == 1
    assert "dep" in proc.stdout.split()
    assert "pybrew install dep" in proc.stderr
    hidden = run_cli("missing", "--hide", "dep")
    assert hidden.returncode == 0


def test_list_named_formula_and_missing_formula(run_cli, sample) -> None:
    assert run_cli("install", "hello", bottle_tag=sample.tag).returncode == 0
    listed = run_cli("list", "hello")
    assert listed.returncode == 0, listed.stderr
    assert "Cellar/hello/1.0/INSTALL_RECEIPT.json" in listed.stdout
    versions = run_cli("list", "--versions", "hello")
    assert versions.returncode == 0
    assert "hello 1.0" in versions.stdout
    missing = run_cli("list", "gcc")
    assert missing.returncode == 1
    assert "No such keg" in missing.stderr


def test_help_command(run_cli) -> None:
    top = run_cli("help")
    assert top.returncode == 0
    assert "usage: pybrew" in top.stdout
    sub = run_cli("help", "list")
    assert sub.returncode == 0
    assert "usage: pybrew list" in sub.stdout
    bare = run_cli()
    assert bare.returncode == 0
    assert "usage: pybrew" in bare.stdout


def test_commands_lists_help_and_list(run_cli) -> None:
    proc = run_cli("commands")
    assert proc.returncode == 0
    assert "install" in proc.stdout
    assert "list" in proc.stdout
    assert "help" in proc.stdout


def test_list_cask_named(run_cli, sample) -> None:
    assert run_cli("install", "--cask", "testapp").returncode == 0
    listed = run_cli("list", "--cask", "testapp")
    assert listed.returncode == 0, listed.stderr
    assert "Caskroom/testapp/1.0" in listed.stdout
    missing = run_cli("list", "--cask", "nope")
    assert missing.returncode == 1
    assert "not installed" in missing.stderr
