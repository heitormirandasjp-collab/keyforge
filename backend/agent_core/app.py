"""Ponto de entrada do KeyForge.

Sobe o servidor local, garante o workspace e abre o navegador na porta certa.
É esse módulo que o PyInstaller transforma em ``keyforge-backend.exe``.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

from . import __version__
from .config import APP_NAME, ConfigStore, default_workspace
from .server import KeyForgeApp, create_server, find_free_port


def frontend_root() -> Path | None:
    """Localiza o build estático do frontend (dev, instalado ou embutido)."""
    candidates: list[Path] = []
    env = os.environ.get("KEYFORGE_FRONTEND")
    if env:
        candidates.append(Path(env))

    if getattr(sys, "frozen", False):
        bundle = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        candidates.append(bundle / "frontend_dist")
        candidates.append(Path(sys.executable).parent / "frontend")

    here = Path(__file__).resolve()
    candidates.append(here.parents[2] / "frontend" / "dist")
    candidates.append(here.parents[1] / "frontend")

    for candidate in candidates:
        if (candidate / "index.html").is_file():
            return candidate
    return None


def _is_our_server(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1.0) as response:
            data = json.loads(response.read().decode("utf-8", "replace"))
        return data.get("app") == APP_NAME
    except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError):
        return False


def _init_console() -> None:
    """Evita UnicodeEncodeError no console do Windows (cp1252/cp850)."""
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def _banner(url: str, workspace: Path, config_file: Path) -> None:
    line = "-" * 62
    print(line)
    print(f"  {APP_NAME} {__version__} — agente de programação com pool de chaves")
    print(line)
    print(f"  Interface : {url}")
    print(f"  Config    : {config_file}")
    print(f"  Workspace : {workspace}")
    print(line)
    print("  Feche esta janela (ou Ctrl+C) para encerrar o servidor.")
    print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="keyforge", description=f"{APP_NAME} — backend local")
    parser.add_argument("--port", type=int, default=None, help="porta fixa (padrão: primeira livre a partir de 8787)")
    parser.add_argument("--host", default="127.0.0.1", help="interface de escuta (padrão: 127.0.0.1)")
    parser.add_argument("--no-browser", action="store_true", help="não abrir o navegador automaticamente")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    args = parser.parse_args(argv)
    _init_console()

    store = ConfigStore()
    workspace = store.data.get("workspace") or str(default_workspace())
    workspace_path = Path(workspace).expanduser()
    workspace_path.mkdir(parents=True, exist_ok=True)
    if not store.data.get("workspace"):
        store.update({"workspace": str(workspace_path.resolve())})

    # Instância já rodando na porta pedida: só abre a interface.
    preferred = args.port or 8787
    if _is_our_server(preferred):
        url = f"http://127.0.0.1:{preferred}/"
        print(f"{APP_NAME} já está rodando em {url}")
        if not args.no_browser:
            webbrowser.open(url)
        return 0

    port = args.port or find_free_port(host=args.host, start=8787)
    if port != preferred:
        try:
            with open(Path(store.path).with_name("last-port.txt"), "w", encoding="utf-8") as handle:
                handle.write(str(port))
        except OSError:
            pass

    static = frontend_root()
    app = KeyForgeApp(store=store, static_root=static)
    try:
        server = create_server(app, port=port, host=args.host)
    except OSError as exc:
        print(f"Não foi possível abrir a porta {port}: {exc}", file=sys.stderr)
        return 1

    url = f"http://{args.host}:{port}/"
    _banner(url, workspace_path, Path(store.path))
    if static is None:
        print("  AVISO: build do frontend nao encontrado - rode 'npm run build' em frontend/.\n")

    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        print("\nEncerrando...")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
