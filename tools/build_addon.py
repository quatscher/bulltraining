"""Stellt das Add-on für einen lokalen Build zusammen (z. B. /addons auf dem Pi, ohne Images aus der Registry).

    python tools/build_addon.py            -> dist/homeassistant/bulltraining
    python tools/build_addon.py <ziel>     -> <ziel>/bulltraining (z. B. \\\\homeassistant\\addons)

Kopiert die Metadaten aus bulltraining/, das Dockerfile und den Quellcode. Die `image:`-Zeile wird entfernt, damit
Home Assistant lokal baut statt das veröffentlichte Image zu laden. Die Version folgt pyproject.toml.
"""
from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
META = ROOT / "bulltraining"


def build(target_parent: Path) -> Path:
    target = target_parent / "bulltraining"
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(META, target)
    for name in ("pyproject.toml", "README.md", "LICENSE", "Dockerfile"):
        shutil.copy2(ROOT / name, target / name)
    shutil.copytree(ROOT / "src", target / "src",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info"))
    version = re.search(r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(encoding="utf-8"), re.M).group(1)
    cfg = target / "config.yaml"
    text = cfg.read_text(encoding="utf-8")
    text = re.sub(r'^version: .*$', f'version: "{version}"', text, flags=re.M)
    text = re.sub(r'^(# .*\n)?image: .*\n', "", text, flags=re.M)
    cfg.write_text(text, encoding="utf-8", newline="\n")
    return target


if __name__ == "__main__":
    parent = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "dist" / "homeassistant"
    parent.mkdir(parents=True, exist_ok=True)
    print(build(parent))
