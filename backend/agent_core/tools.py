"""Ferramentas do agente (ler/escrever arquivo, listar diretório, shell).

Todas as ferramentas são confinadas ao *workspace* configurado. Caminhos relativos
com ``..``, caminhos absolutos (``C:\\Windows\\System32``, ``/etc``), UNC
(``\\\\servidor\\share``) e links simbólicos que apontem para fora são rejeitados.
A leitura fora do workspace é uma exceção explícita e desligada por padrão
(``agent.allowReadOutsideWorkspace``); mesmo ligada, o ``config.json`` local
(que contém as chaves de API) nunca é legível.

Comandos de shell dependem de aprovação da UI (ver ``agent.py``). Sem
``allow_system_commands`` só rodam utilitários internos do shell (``echo``, ``dir``,
``type``…), sem encadeamento capaz de contrabandear outro executável, sem ``..``
e sem expansão de variáveis de ambiente (``%APPDATA%``); com a flag ligada o agente
pode chamar executáveis externos (``python``, ``node``, ``ISCC.exe``…), sempre com
``cwd`` no workspace.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from .config import config_path, log_file

MAX_READ_LINES = 400
MAX_READ_CHARS = 60_000
MAX_FILE_CHARS = 400_000
MAX_OUTPUT_CHARS = 12_000
MAX_COMMAND_CHARS = 4_000
MAX_LIST_ENTRIES = 250
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".next"}

# Utilitários internos do shell liberados mesmo sem "comandos do sistema".
# Qualquer outro executável (python, node, git, ISCC.exe…) exige a flag.
SAFE_COMMANDS = {
    "echo", "printf", "cd", "pwd", "cls", "clear", "dir", "ls", "cat", "type",
    "more", "head", "tail", "sort", "findstr", "grep", "wc", "tree", "ver",
    "mkdir", "md", "rmdir", "rd", "del", "erase", "copy", "move", "ren", "rename",
    "set", "where", "which", "date", "time", "touch",
}

# Separadores que iniciam um novo comando no cmd.exe/sh. `&` sozinho também é
# separador no Windows e antes passava despercebido (``echo ok & python x``).
_COMMAND_SEPARATORS = re.compile(r"&&|\|\||[&|;()\r\n]")
# Expansão de ambiente permitiria ler %APPDATA%\KeyForge\config.json por `type`.
_ENV_EXPANSION = re.compile(
    r"%(?:[A-Za-z_][A-Za-z0-9_]*)%|\$\{?[A-Za-z_][A-Za-z0-9_]*\}?|![A-Za-z_][A-Za-z0-9_]*!"
)
# Flags curtas do shell/cmd (`dir /b`, `del /s /q`, `cd /d`) não são caminhos.
_SHORT_FLAG = re.compile(r"^/[A-Za-z?]{1,4}$")
_ABS_PATH = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\|/|~[\\/])")


class ToolError(Exception):
    pass


TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Lê um arquivo de texto. Retorna as linhas numeradas. "
                "Use start_line/end_line para ler apenas um trecho de arquivos grandes. "
                "Somente arquivos dentro do workspace (config.json local nunca é legível)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Caminho do arquivo (relativo ao workspace ou absoluto)."},
                    "start_line": {"type": "integer", "description": "Primeira linha (1-based)."},
                    "end_line": {"type": "integer", "description": "Última linha (inclusive)."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Cria ou sobrescreve um arquivo do workspace com o conteúdo informado. "
                "Cria diretórios intermediários quando necessário. "
                "Apenas arquivos dentro do workspace são permitidos."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Caminho relativo ao workspace."},
                    "content": {"type": "string", "description": "Conteúdo completo do arquivo."},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": "Lista arquivos e subdiretórios de um diretório do workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Diretório relativo ao workspace (padrão: .)."},
                    "recursive": {"type": "boolean", "description": "Listar subdiretórios recursivamente (2 níveis)."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_shell",
            "description": (
                "Executa um comando no shell do sistema, com o workspace como diretório "
                "atual. Requer aprovação do usuário na interface. Use para build, testes "
                "e inspeção (ex.: 'npm test', 'git status', 'python -m pytest', "
                "'C:\\Program Files (x86)\\Inno Setup 6\\ISCC.exe ...'). "
                "Se 'Permitir execução de comandos do sistema' estiver desativado, "
                "o comando é restrito ao workspace."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Comando a executar."},
                    "timeout_seconds": {
                        "type": "integer",
                        "description": "Tempo máximo de execução em segundos (padrão 120, máx 1800).",
                    },
                },
                "required": ["command"],
            },
        },
    },
]


def _truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n… saída truncada ({len(text) - limit} caracteres omitidos)"


class ToolRunner:
    def __init__(
        self,
        workspace: str | os.PathLike[str],
        shell_timeout: int = 120,
        allow_system_commands: bool = False,
        allow_read_outside_workspace: bool = False,
    ) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.shell_timeout = max(5, int(shell_timeout))
        self.allow_system_commands = bool(allow_system_commands)
        self.allow_read_outside_workspace = bool(allow_read_outside_workspace)
        if not self.workspace.exists():
            self.workspace.mkdir(parents=True, exist_ok=True)

    # -- caminhos ----------------------------------------------------------- #
    def resolve(self, raw: str | None, allow_outside: bool = False) -> Path:
        raw = (raw or ".").strip().strip('"')
        if not raw:
            raw = "."
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = self.workspace / candidate
        try:
            resolved = candidate.resolve()
        except OSError as exc:
            raise ToolError(f"caminho inválido: {exc}") from None
        if not allow_outside:
            if resolved != self.workspace and self.workspace not in resolved.parents:
                raise ToolError(
                    f"acesso negado: '{raw}' está fora do workspace ({self.workspace})"
                )
        return resolved

    def _reject_protected(self, target: Path) -> None:
        """Impede a leitura do config local, mesmo dentro de um workspace global."""
        try:
            protected = {config_path().resolve(), log_file().resolve()}
        except (OSError, ValueError):  # pragma: no cover - config indisponível
            return
        if target in protected:
            raise ToolError(
                "acesso negado: arquivo de configuração local do KeyForge "
                "(contém chaves de API) não pode ser lido pelo agente"
            )

    def rel(self, path: Path) -> str:
        """Caminho relativo em estilo POSIX (estável entre Windows e Linux)."""
        try:
            relative = str(path.relative_to(self.workspace)) or "."
        except ValueError:  # pragma: no cover - defensivo
            relative = str(path)
        return relative.replace(os.sep, "/")

    # -- ferramentas -------------------------------------------------------- #
    def read_file(self, path: str, start_line: int | None = None, end_line: int | None = None) -> str:
        # Fora do workspace só com opt-in explícito; escapes relativos sempre barrados.
        raw = (path or "").strip().strip('"') or "."
        outside = self.allow_read_outside_workspace and Path(raw).expanduser().is_absolute()
        target = self.resolve(path, allow_outside=outside)
        self._reject_protected(target)
        if not target.exists():
            raise ToolError(f"arquivo não encontrado: {target}")
        if target.is_dir():
            raise ToolError(f"'{target}' é um diretório; use list_directory")
        try:
            if target.stat().st_size > 2_000_000:
                raise ToolError("arquivo grande demais para leitura direta (>2MB)")
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise ToolError(f"falha ao ler arquivo: {exc}") from None

        lines = text.splitlines()
        total = len(lines)
        start = max(1, int(start_line or 1))
        end = int(end_line) if end_line else total
        end = min(total, max(start, end))
        if end - start + 1 > MAX_READ_LINES:
            end = start + MAX_READ_LINES - 1

        body = "\n".join(f"{i:>6}| {lines[i - 1]}" for i in range(start, end + 1))
        if len(body) > MAX_READ_CHARS:
            body = body[:MAX_READ_CHARS] + "\n… trecho truncado"
        header = f"{self.rel(target)} — linhas {start}-{end} de {total}"
        return f"{header}\n{body}" if body else f"{header}\n(arquivo vazio)"

    def write_file(self, path: str, content: str) -> str:
        if not isinstance(content, str):
            raise ToolError("'content' deve ser uma string")
        if len(content) > MAX_FILE_CHARS:
            raise ToolError(f"conteúdo grande demais ({len(content)} caracteres)")
        # Escrita SEMPRE restrita ao workspace (allow_outside=False por padrão)
        target = self.resolve(path, allow_outside=False)
        if target == self.workspace:
            raise ToolError("caminho inválido: é o próprio workspace")
        existed = target.exists()
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8", newline="")
        except OSError as exc:
            raise ToolError(f"falha ao escrever: {exc}") from None
        lines = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
        action = "atualizado" if existed else "criado"
        return f"{action}: {self.rel(target)} ({lines} linha(s), {len(content)} caracteres)"

    def list_directory(self, path: str = ".", recursive: bool = False) -> str:
        # list_directory restrito ao workspace
        root = self.resolve(path, allow_outside=False)
        if not root.exists():
            raise ToolError(f"diretório não encontrado: {self.rel(root)}")
        if not root.is_dir():
            raise ToolError(f"'{self.rel(root)}' é um arquivo; use read_file")

        entries: list[str] = []

        def walk(directory: Path, depth: int) -> None:
            try:
                children = sorted(
                    directory.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())
                )
            except OSError:
                return
            for child in children:
                if len(entries) >= MAX_LIST_ENTRIES:
                    return
                name = child.name
                if child.is_dir():
                    if name in SKIP_DIRS:
                        continue
                    entries.append(f"{self.rel(child)}/")
                    if recursive and depth < 2:
                        walk(child, depth + 1)
                else:
                    try:
                        size = child.stat().st_size
                    except OSError:
                        size = 0
                    entries.append(f"{self.rel(child)}  ({size} bytes)")

        walk(root, 0)
        if not entries:
            return f"{self.rel(root)} está vazio"
        suffix = "\n… listagem truncada" if len(entries) >= MAX_LIST_ENTRIES else ""
        return f"conteúdo de {self.rel(root)}:\n" + "\n".join(entries) + suffix

    def run_shell(self, command: str, timeout_seconds: int | None = None) -> str:
        command = (command or "").strip()
        if not command:
            raise ToolError("comando vazio")
        timeout = self.shell_timeout
        if timeout_seconds:
            timeout = max(5, min(int(timeout_seconds), 1800))

        if not self.allow_system_commands:
            self._guard_command(command)

        try:
            completed = subprocess.run(
                command,
                shell=True,
                cwd=str(self.workspace),
                capture_output=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            raise ToolError(f"comando excedeu {timeout}s e foi encerrado") from None
        except OSError as exc:
            raise ToolError(f"falha ao executar: {exc}") from None

        def decode(raw: bytes) -> str:
            for encoding in ("utf-8", "cp1252", "latin-1"):
                try:
                    return raw.decode(encoding)
                except UnicodeDecodeError:
                    continue
            return raw.decode("utf-8", "replace")

        stdout = decode(completed.stdout or b"").strip()
        stderr = decode(completed.stderr or b"").strip()
        parts = [f"$ {command}", f"exit code: {completed.returncode}"]
        if stdout:
            parts.append(f"--- stdout ---\n{stdout}")
        if stderr:
            parts.append(f"--- stderr ---\n{stderr}")
        if not stdout and not stderr:
            parts.append("(sem saída)")
        return _truncate("\n".join(parts))

    def _guard_command(self, command: str) -> None:
        """Bloqueia executáveis externos e caminhos fora do workspace.

        Só roda com ``allow_system_commands=False``. O comando é dividido em
        segmentos em *todos* os separadores do ``cmd.exe`` (``&&``, ``||``, ``&``,
        ``|``, ``;``, parênteses e quebras de linha), então cada palavra é
        validada: executável da lista segura, sem ``..``, sem caminho absoluto
        fora do workspace e sem expansão de variáveis de ambiente.
        """
        if len(command) > MAX_COMMAND_CHARS:
            raise ToolError(f"comando grande demais ({len(command)} caracteres)")
        if "\x00" in command or "\n" in command or "\r" in command:
            raise ToolError(
                "comando bloqueado: quebras de linha e caracteres de controle não são permitidos"
            )
        # Redirecionamentos de descritor são inofensivos e comuns (`2>&1`).
        for harmless in ("2>&1", "1>&2", ">&2"):
            command = command.replace(harmless, " ")
        if _ENV_EXPANSION.search(command):
            raise ToolError(
                "comando bloqueado: expansão de variáveis de ambiente (%VAR%) não é "
                "permitida no modo restrito. Ative 'Permitir execução de comandos do sistema'."
            )

        for segment in _COMMAND_SEPARATORS.split(command):
            parts = segment.strip().split()
            if not parts:
                continue
            first = parts[0].strip("\"'")
            name = re.split(r"[\\/]", first)[-1].lower()
            if name.endswith(".exe"):
                name = name[: -len(".exe")]
            if name not in SAFE_COMMANDS:
                raise ToolError(
                    f"comando bloqueado: '{first}' não é um utilitário interno do shell. "
                    "Ative 'Permitir execução de comandos do sistema' nas configurações "
                    "(agent.allowSystemCommands) para compilar e rodar scripts externos."
                )
            # `cd` sem argumento vai para o HOME no sh; use `pwd`/`dir`.
            if name == "cd" and len(parts) == 1:
                raise ToolError(
                    "comando bloqueado: 'cd' sem diretório. Use 'pwd' (ou 'dir') para ver o "
                    "diretório atual do workspace."
                )
            # `set` sozinho lista TODAS as variáveis de ambiente (podem conter
            # tokens); só a forma `set VAR=valor` é liberada.
            if name == "set" and not any("=" in token for token in parts[1:]):
                raise ToolError(
                    "comando bloqueado: 'set' sem atribuição despejaria as variáveis de ambiente"
                )
            for token in parts:
                self._guard_token(token)

    def _guard_token(self, token: str) -> None:
        """Valida um token que possa representar um caminho."""
        token = token.lstrip("<>").strip("\"'")
        if not token:
            return
        # `set VAR=valor` não é caminho; o valor ainda passa pela checagem abaixo.
        if "=" in token and not token.startswith("="):
            token = token.split("=", 1)[1]
        normalized = token.replace("\\", "/")
        if any(part == ".." for part in normalized.split("/")):
            raise ToolError(
                "comando bloqueado: '..' pode escapar do workspace e não é permitido "
                "no modo restrito"
            )
        if _SHORT_FLAG.match(token):
            return
        looks_like_path = (
            bool(_ABS_PATH.match(token)) or token == "~" or "\\" in token or "/" in token
        )
        if not looks_like_path:
            return
        try:
            candidate = Path(token).expanduser().resolve()
        except (OSError, ValueError):
            raise ToolError(f"caminho inválido no comando: '{token}'") from None
        if candidate != self.workspace and self.workspace not in candidate.parents:
            raise ToolError(
                f"comando contém caminho fora do workspace: '{token}'. "
                "Ative 'Permitir execução de comandos do sistema' nas configurações."
            )

    # -- dispatcher --------------------------------------------------------- #
    def execute(self, name: str, arguments: dict[str, Any]) -> str:
        if name == "read_file":
            return self.read_file(
                arguments.get("path", ""),
                arguments.get("start_line"),
                arguments.get("end_line"),
            )
        if name == "write_file":
            return self.write_file(arguments.get("path", ""), arguments.get("content", ""))
        if name == "list_directory":
            return self.list_directory(
                arguments.get("path") or ".", bool(arguments.get("recursive"))
            )
        if name == "run_shell":
            return self.run_shell(
                arguments.get("command", ""), arguments.get("timeout_seconds")
            )
        raise ToolError(f"ferramenta desconhecida: {name}")


def parse_arguments(raw: str | dict[str, Any] | None) -> dict[str, Any]:
    """Normaliza os argumentos vindos do modelo (string JSON ou objeto)."""
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        raise ToolError(f"argumentos inválidos (JSON): {str(raw)[:200]}") from None
    if not isinstance(parsed, dict):
        raise ToolError("argumentos devem ser um objeto JSON")
    return parsed


REQUIRES_APPROVAL = {"run_shell"}