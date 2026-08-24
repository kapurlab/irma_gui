import json
import os
from pathlib import Path
from typing import Any, Dict, Optional


def _user_config_dir() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    if xdg:
        return Path(xdg) / "irma_gui"
    return Path.home() / ".config" / "irma_gui"


DATA_DIR = _user_config_dir()
CONFIG_PATH = DATA_DIR / "config.json"

# The multi-user "shared projects" root, if this deployment has one. A laptop
# normally does not — only a lab server or an OOD site. Never assume a path:
# probe in order of authority and fall back to None (no shared root) rather than
# a fictional one, so macOS/WSL users aren't shown a directory that cannot exist.
#   1. BDTOOLS_SHARED_PROJECTS_ROOT — exported by the launcher, which resolved it
#      from the machine's recorded site config. An explicitly empty value is
#      authoritative: it DISABLES the shared root.
#   2. the user's own `shared_projects_root` setting — see shared_projects_root()
# There is no step 3. A site supplies its own value (bdtools records it in
# <BDTOOLS_HOME>/site.conf); this file contains no path of its own, so the same
# release is correct on macOS, WSL, Linux and OOD without editing.
#
# This used to be one lab server's projects path, guarded by is_dir(). The guard
# kept the value out of config.json off that server, but the literal still
# decided what "shared" MEANT: any other site with its own shared root got no
# shared projects at all, silently, because the only path this file would accept
# was one it could never have.
_ENV_SHARED_PROJECTS_ROOT = "BDTOOLS_SHARED_PROJECTS_ROOT"


def _default_shared_projects_root() -> str:
    env = os.environ.get(_ENV_SHARED_PROJECTS_ROOT)
    return env.strip() if env is not None else ""


_DEFAULT_SHARED_PROJECTS_ROOT = _default_shared_projects_root()


def shared_projects_root() -> Optional[Path]:
    """The resolved shared-projects root, or None when this deployment has none.

    Read through this rather than a module constant, so the Settings value is
    honoured: main.py used to carry its own hard-coded literal, which meant
    setting `shared_projects_root` in the GUI changed what Settings displayed and
    nothing about where projects were discovered.

    Returns None — never Path("") — because Path("") is Path("."), the current
    working directory. An "unset" sentinel that silently means "look in ." would
    turn a missing shared root into project lookups against wherever uvicorn
    happens to have been started."""
    env = os.environ.get(_ENV_SHARED_PROJECTS_ROOT)
    if env is not None:
        return Path(env.strip()) if env.strip() else None
    try:
        configured = str(load_config().get("shared_projects_root", "") or "").strip()
    except Exception:
        configured = ""
    if configured:
        return Path(configured)
    return Path(_DEFAULT_SHARED_PROJECTS_ROOT) if _DEFAULT_SHARED_PROJECTS_ROOT else None


# GenoFLU reference DB. Always empty by default — run_genoflu.py resolves the set
# bundled inside the conda `genoflu` package (relative to genoflu.py), which is
# the version-pinned location recorded in each run's provenance. Set this only to
# pin an out-of-tree reference set (e.g. a newer genotype key).
#
# It used to be seeded from one lab server's genoflu dependencies directory when
# that directory happened to exist. On the machine it was written for that was
# right; everywhere else the literal reached config.json anyway on releases
# before the existence guard, and once persisted no later release removes it —
# load_config() only fills in keys that are MISSING. A 2026-08-24 HPC run was
# still invoked with a --genoflu-db under /srv on a cluster that has no such
# tree, for exactly that reason. There is no literal left to inherit.
_GENOFLU_DB_DEFAULT = ""

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
