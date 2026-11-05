"""Seletor nativo de pasta (tkinter, biblioteca padrão).

O navegador não expõe o caminho absoluto de um diretório escolhido em
``<input type="file" webkitdirectory>``, então o KeyForge abre o diálogo nativo
do sistema **no backend local** e devolve o caminho completo para a interface.

Se não houver ambiente gráfico (servidor headless), levanta
``DialogUnavailable`` e a UI orienta o usuário a digitar o caminho.
"""
from __future__ import annotations

import os
import sys


class DialogUnavailable(RuntimeError):
    """Não foi possível abrir o diálogo nativo nesta máquina."""


def pick_directory(
    initial: str | None = None,
    title: str = "Selecione a pasta de trabalho do KeyForge",
) -> str | None:
    """Abre o diálogo de seleção de pasta. Retorna ``None`` se cancelado."""
    if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
        raise DialogUnavailable(
            "sem ambiente gráfico para abrir o seletor de pastas — digite o caminho completo"
        )

    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception as exc:  # pragma: no cover - depende da instalação local
        raise DialogUnavailable(f"tkinter indisponível: {exc}") from None

    try:
        root = tk.Tk()
    except Exception as exc:  # pragma: no cover - depende da sessão gráfica
        raise DialogUnavailable(f"não foi possível abrir a janela do seletor: {exc}") from None

    root.withdraw()
    try:
        root.attributes("-topmost", True)
    except Exception:  # pragma: no cover - alguns WM não suportam
        pass
    try:
        chosen = filedialog.askdirectory(
            parent=root,
            title=title,
            initialdir=initial or "",
            mustexist=False,
        )
    finally:
        try:
            root.destroy()
        except Exception:  # pragma: no cover - janela já destruída
            pass

    chosen = (chosen or "").strip()
    return chosen or None
