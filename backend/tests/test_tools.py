from __future__ import annotations

import pytest

from agent_core.tools import ToolError, ToolRunner, parse_arguments


@pytest.fixture
def runner(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    return ToolRunner(workspace)


def test_write_read_and_list(runner):
    runner.write_file("src/app.py", "print('oi')\n")
    output = runner.read_file("src/app.py")
    assert "src/app.py" in output and "1|" in output and "print('oi')" in output

    listing = runner.list_directory(".", recursive=True)
    assert "src/" in listing and "src/app.py" in listing


def test_write_overwrites_and_reports(runner):
    runner.write_file("a.txt", "um")
    message = runner.write_file("a.txt", "dois\n")
    assert message.startswith("atualizado") and "1 linha" in message
    assert runner.read_file("a.txt").endswith("dois")


def test_path_escape_is_denied(runner):
    with pytest.raises(ToolError):
        runner.read_file("../../etc/passwd")
    with pytest.raises(ToolError):
        runner.write_file("../fora.txt", "x")
    assert not (runner.workspace.parent / "fora.txt").exists()


def test_read_missing_file_raises(runner):
    with pytest.raises(ToolError):
        runner.read_file("nao-existe.txt")


def test_read_respects_line_window(runner):
    runner.write_file("big.txt", "\n".join(f"linha {i}" for i in range(1, 51)))
    output = runner.read_file("big.txt", start_line=10, end_line=12)
    assert "linha 10" in output and "linha 12" in output
    assert "linha 13" not in output


def test_run_shell_executes_in_workspace(runner):
    output = runner.run_shell("echo keyforge")
    assert "keyforge" in output
    assert "exit code: 0" in output


def test_run_shell_timeout(tmp_path):
    workspace = tmp_path / "ws-timeout"
    workspace.mkdir()
    # python é executável externo: precisa da flag de comandos do sistema.
    permissive = ToolRunner(workspace, allow_system_commands=True)
    with pytest.raises(ToolError):
        permissive.run_shell("python -c \"import time; time.sleep(5)\"", timeout_seconds=5)


def test_system_commands_require_the_flag(tmp_path):
    workspace = tmp_path / "ws-flag"
    workspace.mkdir()

    restricted = ToolRunner(workspace)
    with pytest.raises(ToolError) as blocked:
        restricted.run_shell("python --version")
    assert "Permitir execução de comandos do sistema" in str(blocked.value)

    allowed = ToolRunner(workspace, allow_system_commands=True)
    output = allowed.run_shell("python --version")
    assert "exit code: 0" in output
    assert "Python 3" in output


def test_system_commands_flag_keeps_cwd_in_workspace(tmp_path):
    workspace = tmp_path / "ws-cwd"
    workspace.mkdir()
    allowed = ToolRunner(workspace, allow_system_commands=True)
    output = allowed.run_shell(
        "python -c \"import os; print(os.getcwd())\"", timeout_seconds=30
    )
    assert str(workspace.resolve()) in output.replace("\\\\", "\\")


def test_read_absolute_path_outside_workspace_is_denied(runner, tmp_path):
    outside = tmp_path / "fora-do-workspace.txt"
    outside.write_text("segredo", encoding="utf-8")
    with pytest.raises(ToolError) as blocked:
        runner.read_file(str(outside))
    assert "fora do workspace" in str(blocked.value)


def test_read_outside_workspace_requires_explicit_opt_in(tmp_path):
    workspace = tmp_path / "ws-optin"
    workspace.mkdir()
    outside = tmp_path / "consulta.txt"
    outside.write_text("consulta livre", encoding="utf-8")

    runner = ToolRunner(workspace, allow_read_outside_workspace=True)
    assert "consulta livre" in runner.read_file(str(outside))

    # Mesmo com o opt-in, escape relativo continua bloqueado.
    with pytest.raises(ToolError):
        runner.read_file("../../../etc/passwd")


def test_read_local_config_is_always_denied(tmp_path, monkeypatch):
    config_file = tmp_path / "config.json"
    config_file.write_text('{"providers": {}}', encoding="utf-8")
    monkeypatch.setenv("KEYFORGE_CONFIG", str(config_file))

    runner = ToolRunner(tmp_path)
    with pytest.raises(ToolError) as blocked:
        runner.read_file("config.json")
    assert "configuração local" in str(blocked.value)


def test_shell_chain_cannot_smuggle_external_command(runner):
    with pytest.raises(ToolError):
        runner.run_shell("echo ok && python --version")


def test_shell_single_ampersand_cannot_smuggle_external_command(runner):
    with pytest.raises(ToolError):
        runner.run_shell("echo ok & python --version")


def test_shell_rejects_parent_directory(runner):
    with pytest.raises(ToolError):
        runner.run_shell("dir ..")
    with pytest.raises(ToolError):
        runner.run_shell("type ../segredo.txt")


def test_shell_rejects_home_shorthand(runner):
    with pytest.raises(ToolError):
        runner.run_shell("cd ~")
    with pytest.raises(ToolError):
        runner.run_shell("cd")


def test_shell_rejects_environment_expansion(runner):
    with pytest.raises(ToolError):
        runner.run_shell("type %APPDATA%\\KeyForge\\config.json")


def test_shell_rejects_absolute_path_outside_workspace(runner, tmp_path):
    outside = tmp_path / "fora.txt"
    outside.write_text("x", encoding="utf-8")
    with pytest.raises(ToolError) as blocked:
        runner.run_shell(f'type "{outside}"')
    assert "fora do workspace" in str(blocked.value)


def test_shell_does_not_break_descriptor_redirection(runner):
    output = runner.run_shell("echo ok 2>&1")
    assert "ok" in output


def test_write_cannot_use_absolute_windows_path(tmp_path):
    workspace = tmp_path / "ws-win"
    workspace.mkdir()
    runner = ToolRunner(workspace)
    with pytest.raises(ToolError):
        runner.write_file("C:\\Windows\\System32\\keyforge.txt", "x")


def test_parse_arguments_variants():
    assert parse_arguments('{"path": "a"}') == {"path": "a"}
    assert parse_arguments({"path": "a"}) == {"path": "a"}
    assert parse_arguments(None) == {}
    with pytest.raises(ToolError):
        parse_arguments("{invalido}")


def test_unknown_tool_raises(runner):
    with pytest.raises(ToolError):
        runner.execute("rm_rf", {})
