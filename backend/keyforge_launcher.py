"""Entrypoint de empacotamento (PyInstaller).

Executa ``agent_core.app.main`` com caminhos absolutos, evitando imports
relativos quando o processo roda congelado.
"""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent_core.app import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
