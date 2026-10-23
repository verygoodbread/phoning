"""Unit tests for ``phoning connect`` (phoning/connect.py): any harness onto a line."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from phoning import cli, connect

EXE = "/opt/bin/phoning"
PATHENV = "/opt/bin:/usr/bin:/bin"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: Any) -> Path:
    """A fake $HOME with one line ``agent-a`` under the default phoning home."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("PHONING_HOME", raising=False)
    monkeypatch.setattr(connect, "phoning_command", lambda: EXE)
    monkeypatch.setattr(connect, "server_path_env", lambda: PATHENV)
    (tmp_path / ".phoning" / "agent-a" / "mailbox").mkdir(parents=True)
    return tmp_path


def _connect(key: str, *extra: str, run: Any = None) -> int:
    argv = [key, "agent-a", *extra]
    if run is None:
        return connect.main(argv)
    args = connect._parser().parse_args(argv)
    return connect.connect(connect.BY_KEY[key], "agent-a", connect._home(None), project=args.project,
                           dry_run=args.dry_run, mcp=not args.no_mcp, instructions=not args.no_instructions,
                           run=run)


def _backups(d: Path) -> list[str]:
    return sorted(p.name for p in d.iterdir() if ".bak-phoning-" in p.name)


def test_list_names_every_harness(capsys: Any) -> None:
    assert connect.main(["list", "--json"]) == 0
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    keys = [r["harness"] for r in rows]
    for k in ("claude", "codex", "cursor", "opencode", "gemini", "muse", "kimi", "openclaw", "deepseek-harness",
              "generic"):
        assert k in keys
    assert all({"installed", "mcp", "instructions", "checked"} <= set(r) for r in rows)


def test_unknown_line_is_refused_and_writes_nothing(home: Path, capsys: Any) -> None:
    assert connect.main(["gemini", "nobody"]) == connect.EXIT_USAGE
    assert "phoning init nobody" in capsys.readouterr().err
    assert not (home / ".gemini").exists()


def test_json_mcpservers_merges_and_is_idempotent(home: Path, capsys: Any) -> None:
    cfg = home / ".cursor" / "mcp.json"
    cfg.parent.mkdir()
    cfg.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}, "keep": 1}))
    assert _connect("cursor", "--no-instructions") == 0
    data = json.loads(cfg.read_text())
    assert data["keep"] == 1 and data["mcpServers"]["other"] == {"command": "x"}
    assert data["mcpServers"]["phoning"] == {
        "command": EXE,
        "args": ["mcp", "agent-a", "--home", str(home / ".phoning"), "--session", "agent-a-mcp"],
        "env": {"PATH": PATHENV},
    }
    assert len(_backups(cfg.parent)) == 1
    capsys.readouterr()
    assert _connect("cursor", "--no-instructions") == 0
    assert "already has the phoning server" in capsys.readouterr().out
    assert len(_backups(cfg.parent)) == 1


def test_json_with_comments_is_never_overwritten(home: Path, capsys: Any) -> None:
    cfg = home / ".gemini" / "settings.json"
    cfg.parent.mkdir()
    original = '{ // my settings\n "theme": "dark" }'
    cfg.write_text(original)
    assert _connect("gemini", "--no-instructions") == connect.EXIT_FAIL
    assert cfg.read_text() == original
    assert '"mcpServers"' in capsys.readouterr().err


def test_opencode_uses_its_own_mcp_shape(home: Path) -> None:
    assert _connect("opencode") == 0
    data = json.loads((home / ".config" / "opencode" / "opencode.json").read_text())
    entry = data["mcp"]["phoning"]
    assert entry["type"] == "local" and entry["enabled"] is True
    assert entry["command"][0] == EXE and entry["command"][1:3] == ["mcp", "agent-a"]
    assert entry["environment"] == {"PATH": PATHENV}
    assert "voicemail agent-a" in (home / ".config" / "opencode" / "AGENTS.md").read_text()


def test_deepseek_harness_inserts_one_mcp_row_and_global_instructions(home: Path) -> None:
    patch = home / ".dsh" / "profiles" / "desktop" / "cordis.patch.yml"
    patch.parent.mkdir(parents=True)
    patch.write_text("- id: agent-default-model\n  config:\n    model: deepseek-flash\n")
    assert _connect("deepseek-harness") == 0
    assert _connect("deepseek-harness") == 0
    text = patch.read_text()
    assert text.startswith("- id: agent-default-model\n")
    assert text.count(connect.YAML_BEGIN) == 1 and text.count("- insert:") == 1
    assert "name: '@deepseek-ai/dsh-mcp-client'" in text and "serverName: phoning" in text
    assert f'command: "{EXE}"' in text and '"agent-a-mcp"' in text
    agents = (home / ".dsh" / "AGENTS.md").read_text()
    assert agents.count(connect.BEGIN) == 1 and "## Your phone line (phoning)" in agents


def test_instruction_block_is_upserted_and_keeps_the_rest(home: Path) -> None:
    md = home / ".gemini" / "GEMINI.md"
    md.parent.mkdir()
    md.write_text("# My rules\n\nBe nice.\n")
    assert _connect("gemini", "--no-mcp") == 0
    first = md.read_text()
    assert first.startswith("# My rules\n\nBe nice.\n\n" + connect.BEGIN)
    assert "phoning voicemail agent-a --reader agent-a --sanitized" in first
    assert "PHONING_HOME=" not in first
    assert _connect("gemini", "--no-mcp") == 0
    assert md.read_text() == first


def test_custom_home_is_spelled_out_in_the_commands(home: Path, monkeypatch: Any) -> None:
    other = home / "elsewhere"
    (other / "agent-a" / "mailbox").mkdir(parents=True)
    monkeypatch.setenv("PHONING_HOME", str(other))
    assert connect.main(["gemini", "agent-a", "--no-mcp"]) == 0
    assert f"PHONING_HOME={other} phoning voicemail" in (home / ".gemini" / "GEMINI.md").read_text()


def test_project_flag_writes_the_projects_agents_md(home: Path) -> None:
    proj = home / "proj"
    proj.mkdir()
    assert _connect("cursor", "--no-mcp", "--project", str(proj)) == 0
    assert connect.BEGIN in (proj / "AGENTS.md").read_text()


def test_project_only_harness_without_project_prints_the_block(home: Path, capsys: Any) -> None:
    assert _connect("kimi") == 0
    out = capsys.readouterr().out
    assert '"mcpServers"' in out and "[mcp_servers.phoning]" in out and connect.BEGIN in out
    assert not list(home.glob("**/AGENTS.md"))


class _Runner:
    def __init__(self, rc: int = 0, err: str = "") -> None:
        self.calls: list[list[str]] = []
        self.rc, self.err = rc, err

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        return subprocess.CompletedProcess(argv, self.rc, "", self.err)


def test_claude_puts_the_name_before_the_variadic_env_flag(home: Path) -> None:
    run = _Runner()
    assert _connect("claude", run=run) == 0
    (argv,) = run.calls
    assert argv[:7] == ["claude", "mcp", "add", "--scope", "user", "phoning", "-e"]
    assert argv[argv.index("--") + 1:] == [EXE, "mcp", "agent-a", "--home", str(home / ".phoning"), "--session",
                                           "agent-a-mcp"]
    assert connect.BEGIN in (home / ".claude" / "CLAUDE.md").read_text()


def test_codex_and_openclaw_argv(home: Path) -> None:
    run = _Runner()
    assert _connect("codex", "--no-instructions", run=run) == 0
    assert _connect("openclaw", "--no-instructions", run=run) == 0
    codex, oc = run.calls
    assert codex[:6] == ["codex", "mcp", "add", "--env", f"PATH={PATHENV}", "phoning"] and codex[6] == "--"
    assert oc[:5] == ["openclaw", "mcp", "add", "phoning", "--command"]
    assert "--arg=--home" in oc and "--arg=mcp" in oc and f"--env=PATH={PATHENV}" in oc


def test_cli_failure_and_already_exists(home: Path, capsys: Any) -> None:
    assert _connect("codex", "--no-instructions", run=_Runner(1, "boom")) == connect.EXIT_FAIL
    assert "boom" in capsys.readouterr().err
    assert _connect("claude", "--no-instructions", run=_Runner(1, "MCP server phoning already exists")) == 0
    assert "already registered" in capsys.readouterr().out


def test_dry_run_changes_nothing_and_runs_nothing(home: Path, capsys: Any) -> None:
    run = _Runner()
    for key in ("claude", "cursor", "deepseek-harness", "opencode"):
        assert _connect(key, "--dry-run", run=run) == 0
    assert run.calls == []
    assert not any(p.is_file() for p in home.rglob("*") if ".phoning" not in p.parts)
    assert "would:" in capsys.readouterr().out


def test_snippet_prints_valid_json_and_toml(home: Path, capsys: Any) -> None:
    assert connect.main(["snippet", "agent-a", "--format", "mcp"]) == 0
    out = capsys.readouterr().out
    start = out.index("{")
    end = out.index("\n\nMCP server, TOML")
    assert json.loads(out[start:end])["mcpServers"]["phoning"]["command"] == EXE
    assert '[mcp_servers.phoning]\ncommand = "/opt/bin/phoning"' in out


def test_upsert_block_appends_with_one_blank_line() -> None:
    b = "<a>\nx\n<b>\n"
    assert connect.upsert_block("", b, "<a>", "<b>") == b
    assert connect.upsert_block("top", b, "<a>", "<b>") == "top\n\n" + b
    assert connect.upsert_block("top\n", b, "<a>", "<b>") == "top\n\n" + b
    assert connect.upsert_block("top\n" + b + "tail\n", "<a>\ny\n<b>\n", "<a>", "<b>") == "top\n<a>\ny\n<b>\ntail\n"


def test_cli_routes_connect_and_the_hook_up_alias(monkeypatch: Any) -> None:
    seen: list[list[str]] = []
    monkeypatch.setattr(connect, "main", lambda argv=None: seen.append(list(argv)) or 0)
    assert cli.main(["connect", "list"]) == 0
    assert cli.main(["hook-up", "codex", "x"]) == 0
    assert seen == [["list"], ["codex", "x"]]
    assert os.path.basename(connect.__file__) == "connect.py"


def test_block_points_at_the_phonebook_and_names_the_room(home: Path) -> None:
    assert connect.main(["gemini", "agent-a", "--no-mcp", "--room", "lobby"]) == 0
    text = (home / ".gemini" / "GEMINI.md").read_text()
    assert "`phoning phonebook list`" in text
    assert "Your team's party line is `lobby`" in text and "--room lobby" in text
    assert "party line is" not in connect.instruction_block("agent-a", str(home / ".phoning"))


def test_codex_config_writes_an_overlay_file_instead_of_running_codex(home: Path, monkeypatch: Any) -> None:
    overlay = home / ".codex" / "work.config.toml"
    overlay.parent.mkdir()
    overlay.write_text('model = "x"\n')
    run = _Runner()
    monkeypatch.setattr(connect, "_run", run)
    assert connect.main(["codex", "agent-a", "--codex-config", str(overlay), "--no-instructions"]) == 0
    assert connect.main(["codex", "agent-a", "--codex-config", str(overlay), "--no-instructions"]) == 0
    assert run.calls == []
    text = overlay.read_text()
    assert text.startswith('model = "x"\n') and text.count("[mcp_servers.phoning]") == 1
    assert f'command = "{EXE}"' in text and '"agent-a-mcp"' in text
    assert not (home / ".codex" / "config.toml").exists()
