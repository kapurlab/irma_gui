import json
import os
from pathlib import Path
from typing import Any, Dict


def _user_config_dir() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    if xdg:
        return Path(xdg) / "irma_gui"
    return Path.home() / ".config" / "irma_gui"


DATA_DIR = _user_config_dir()
CONFIG_PATH = DATA_DIR / "config.json"

_SHARED_PROJECTS_ROOT = Path("/srv/kapurlab/projects")
_DEFAULT_SHARED_PROJECTS_ROOT = (
    str(_SHARED_PROJECTS_ROOT) if _SHARED_PROJECTS_ROOT.is_dir() else ""
)


def _first_existing(*paths: str) -> str:
    """Return the first path that exists, else the first candidate (so the
    default is informative even on a fresh box)."""
    for p in paths:
        if p and Path(p).exists():
            return p
    return paths[0] if paths else ""


# GenoFLU reference DB. Empty by default — run_genoflu.py resolves the set
# bundled inside the conda `genoflu` package (relative to genoflu.py). Set this
# only to pin an out-of-tree reference set (e.g. a newer genotype key).
_GENOFLU_DB_DEFAULT = _first_existing(
    "/srv/kapurlab/databases/genoflu/dependencies",
    "",
)

DEFAULTS: Dict[str, Any] = {
    "projects_root": str(Path.home() / "projects"),
    "shared_projects_root": _DEFAULT_SHARED_PROJECTS_ROOT,
    "saved_project_roots": [],
    # IRMA module to assemble with: FLU (influenza A/B) or CoV (SARS-CoV-2).
    "irma_module": "FLU",
    # GenoFLU genotyping of the assembled influenza-A genome.
    "run_genoflu": True,
    "genoflu_db": _GENOFLU_DB_DEFAULT,
    "genoflu_pident": 98.0,
}


def load_config() -> Dict[str, Any]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.exists():
        save_config(DEFAULTS)
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    for k, v in DEFAULTS.items():
        cfg.setdefault(k, v)
    return cfg


def save_config(cfg: Dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, sort_keys=True)
