"""Stellt das Home-Assistant-Add-on zusammen: Metadaten aus homeassistant/bulltraining + Quellcode.

    python homeassistant/build_addon.py            -> dist/homeassistant/bulltraining
    python homeassistant/build_addon.py <ziel>     -> <ziel>/bulltraining (z. B. \\\\homeassistant\\addons)

Der Zielordner wird ersetzt. Die Add-on-Version folgt pyproject.toml, damit Home Assistant ein Update erkennt.
"""
from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
META = ROOT / "homeassistant" / "bulltraining"


def build(target_parent: Path) -> Path:
    target = target_parent / "bulltraining"
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(META, target)
    shutil.copy2(ROOT / "pyproject.toml", target / "pyproject.toml")
    shutil.copytree(ROOT / "src", target / "src",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info"))
    version = re.search(r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(encoding="utf-8"), re.M).group(1)
    cfg = target / "config.yaml"
    cfg.write_text(re.sub(r'^version: .*$', f'version: "{version}"', cfg.read_text(encoding="utf-8"), flags=re.M),
                   encoding="utf-8", newline="\n")
    return target


if __name__ == "__main__":
    parent = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "dist" / "homeassistant"
    parent.mkdir(parents=True, exist_ok=True)
    print(build(parent))
