"""Copia la página de ui.py a web/public/index.html (una sola fuente para la UI local y la nube)."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import ui  # noqa: E402

out = ROOT / "web" / "public" / "index.html"
out.write_text(ui.PAGE)
print(f"{out} actualizado")
