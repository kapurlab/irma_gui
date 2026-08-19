"""
IRMA GUI — FastAPI backend.

Serves the React SPA from frontend/dist/ and provides:
  /api/projects                         — list shared + personal projects
  /api/projects/{n}/{inputs,upload,link-local,samples,sra/download}
  /api/projects/{n}/metadata            — get/save per-sample submission metadata
  /api/projects/{n}/metadata.xlsx       — download the metadata workbook
  /api/projects/{n}/metadata/upload     — replace the metadata workbook (Excel)
  /api/config                           — get/set user config
  /api/browse-dirs                      — project-root folder picker
  /api/run                              — start an irma_pipeline.py run
  /api/jobs, /api/jobs/{id}, /api/jobs/{id}/log (SSE), /api/jobs/{id}/results
  /api/projects/{n}/samples/{s}/irma-results — per-sample result files
  /api/projects/{n}/samples/{s}/irma-table   — parsed assembly/genotype summary

A sibling of vsnp_gui / kraken_id_parse_gui / amr_plus_gui / genoflu_gui sharing
their project layout. All URLs are served from / (uvicorn behind OOD's rnode
proxy — relative paths only).
"""

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiofiles
from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import load_config, save_config
from .jobs import JobManager
from .request_safety import install_request_safety
from .sra import (
    SRAExpansionError,
    build_download_script,
    expand_accessions_with_mapping,
    write_crosswalk_tsv,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent          # /srv/kapurlab/tools/irma_gui
_BIN_DIR = _REPO_ROOT / "bin"
_FRONTEND_DIST = _REPO_ROOT / "frontend" / "dist"
_SHARED_PROJECTS = Path("/srv/kapurlab/projects")
_JOBS_DIR = _REPO_ROOT / "backend" / "jobs"

# The pipeline's bin/ holds the metadata helpers the metadata routes reuse, so
# the GUI and the pipeline build submission headers from one source of truth.
if str(_BIN_DIR) not in sys.path:
    sys.path.insert(0, str(_BIN_DIR))
import metadata as meta_mod  # noqa: E402

# Per-tool subdirectory inside each project (mirrors amr/, genoflu/).
_TOOL_SUBDIR = "irma"

app = FastAPI(title="IRMA GUI")
install_request_safety(app)

job_manager = JobManager(_JOBS_DIR)

_SCOPE_SHARED = "shared"
_SCOPE_PERSONAL = "personal"


# ---------------------------------------------------------------------------
# Project listing
# ---------------------------------------------------------------------------
def _safe_mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime if p.is_dir() else 0
    except PermissionError:
        return 0


def _count_project_reads(download_dir: Path, step1_dir: Path) -> int:
    seen: set = set()
    candidates = []
    if download_dir.is_dir():
        candidates += download_dir.rglob("*.fastq.gz")
    if step1_dir.is_dir():
        candidates += step1_dir.glob("*/*.fastq.gz")
    for f in candidates:
        if "_unmapped_" in f.name:
            continue
        try:
            key = f.resolve()
        except OSError:
            key = f
        seen.add(key)
    return len(seen)


def _list_projects_from_root(root: Path, scope: str) -> List[Dict]:
    if not root.is_dir():
        return []
    projects = []
    try:
        entries = sorted(root.iterdir(), key=_safe_mtime, reverse=True)
    except PermissionError:
        return []
    for p in entries:
        try:
            if not p.is_dir() or p.name.startswith("."):
                continue
        except PermissionError:
            continue
        download_dir = p / "download"
        try:
            fastq_count = _count_project_reads(download_dir, p / "step1")
        except PermissionError:
            fastq_count = -1
        irma_runs = []
        irma_dir = p / _TOOL_SUBDIR
        try:
            if irma_dir.is_dir():
                irma_runs = [d.name for d in sorted(irma_dir.iterdir()) if d.is_dir()]
        except PermissionError:
            pass
        projects.append({
            "name": p.name,
            "path": str(p),
            "scope": scope,
            "fastq_count": fastq_count,
            "irma_runs": irma_runs,
        })
    return projects


def _get_project_dir(name: str) -> Optional[Path]:
    if "/" in name or name.startswith("."):
        return None
    cfg = load_config()
    for root in [_SHARED_PROJECTS, Path(cfg.get("projects_root", ""))]:
        candidate = root / name
        if candidate.is_dir():
            return candidate
    return None


# ---------------------------------------------------------------------------
# Project creation (shared on-disk skeleton with the sibling tools)
# ---------------------------------------------------------------------------
_PROJECT_NAME_OK_CHARSET = re.compile(r"^[A-Za-z0-9._-]+$")


def _normalize_project_name(name: str) -> str:
    if not isinstance(name, str):
        raise ValueError("Project name must be a string")
    cleaned = re.sub(r"\s+", "_", name.strip())
    if not cleaned:
        raise ValueError("Project name is empty")
    if cleaned.startswith("."):
        raise ValueError("Project name cannot start with '.'")
    if len(cleaned) > 100:
        raise ValueError("Project name too long (max 100 characters)")
    if not _PROJECT_NAME_OK_CHARSET.match(cleaned):
        bad = sorted(set(ch for ch in cleaned if not re.match(r"[A-Za-z0-9._-]", ch)))
        raise ValueError(
            f"Project name contains unsupported characters: {''.join(bad)!r}. "
            "Only letters, digits, _ - . are allowed (spaces become underscores)."
        )
    return cleaned


def _ensure_project_dirs(project_dir: Path) -> None:
    (project_dir / "download").mkdir(parents=True, exist_ok=True)
    (project_dir / _TOOL_SUBDIR).mkdir(parents=True, exist_ok=True)
    # vSNP-compatible layout so the project is shared cleanly between tools.
    (project_dir / "step1").mkdir(parents=True, exist_ok=True)
    (project_dir / "step2" / "vcf_source").mkdir(parents=True, exist_ok=True)
    (project_dir / f"{project_dir.name}_VCFs").mkdir(parents=True, exist_ok=True)


def _now_iso() -> str:
    from datetime import datetime
    return datetime.now().isoformat(timespec="seconds")


def _create_project(name: str, scope: str) -> Path:
    name = _normalize_project_name(name)
    cfg = load_config()
    root = _SHARED_PROJECTS if scope == _SCOPE_SHARED else Path(
        cfg.get("projects_root", "") or (Path.home() / "projects"))
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ValueError(f"Cannot create projects root {root}: {exc}")
    project_dir = root / name
    if project_dir.exists():
        raise ValueError(f"Project already exists: {name}")
    try:
        _ensure_project_dirs(project_dir)
    except PermissionError:
        raise ValueError(
            f"No permission to create a project under {root}. "
            "Shared projects require lab write access; create it as a personal "
            "project instead.")
    try:
        with open(project_dir / "project.json", "w", encoding="utf-8") as f:
            json.dump({"name": name, "created_at": _now_iso(), "status": "created"},
                      f, indent=2, sort_keys=True)
    except OSError:
        pass
    return project_dir


# Matches _R1/_R2 (with optional _001 etc.) or _1/_2 immediately before .fastq.gz
_READ_TAG_RE = re.compile(r'(?:_R([12])(?:_\d+)?|_([12]))\.fastq\.gz$', re.IGNORECASE)


def _strip_read_tag(filename: str):
    m = _READ_TAG_RE.search(filename)
    if m:
        tag = m.group(1) or m.group(2)
        return filename[:m.start()], tag
    return (filename[:-len(".fastq.gz")] if filename.endswith(".fastq.gz") else filename), None


def _list_fastq_pairs(download_dir: Path) -> List[Dict]:
    try:
        all_fq = sorted(download_dir.glob("*.fastq.gz"))
    except PermissionError:
        return []
    groups: Dict[str, Dict] = {}
    for fq in all_fq:
        base, tag = _strip_read_tag(fq.name)
        if base not in groups:
            groups[base] = {"r1": None, "r2": None, "extras": []}
        g = groups[base]
        if tag == "1":
            g["r1"] = fq
        elif tag == "2":
            g["r2"] = fq
        else:
            g["extras"].append(fq)
    pairs = []
    for base, g in groups.items():
        r1, r2 = g["r1"], g["r2"]
        if r1 or r2:
            eff_r1 = r1 or r2
            eff_r2 = r2 if r1 else None
            pairs.append({
                "sample": base, "paired": bool(r1 and r2),
                "r1": str(eff_r1), "r1_name": eff_r1.name, "r1_size": eff_r1.stat().st_size,
                "r2": str(eff_r2) if eff_r2 else None,
                "r2_name": eff_r2.name if eff_r2 else None,
                "r2_size": eff_r2.stat().st_size if eff_r2 else None,
            })
        for fq in g["extras"]:
            pairs.append({
                "sample": fq.name[:-len(".fastq.gz")], "paired": False,
                "r1": str(fq), "r1_name": fq.name, "r1_size": fq.stat().st_size,
                "r2": None, "r2_name": None, "r2_size": None,
            })
    return pairs


# ---------------------------------------------------------------------------
# API routes — projects / inputs
# ---------------------------------------------------------------------------
@app.get("/api/projects")
def api_list_projects():
    cfg = load_config()
    projects = _list_projects_from_root(_SHARED_PROJECTS, _SCOPE_SHARED)
    personal_root = Path(cfg.get("projects_root", ""))
    if personal_root != _SHARED_PROJECTS:
        personal = _list_projects_from_root(personal_root, _SCOPE_PERSONAL)
        seen = {p["name"] for p in projects}
        projects += [p for p in personal if p["name"] not in seen]
    return JSONResponse(projects)


class ProjectCreate(BaseModel):
    name: str
    scope: Optional[str] = None


@app.post("/api/projects")
def api_create_project(payload: ProjectCreate):
    scope = (payload.scope or _SCOPE_PERSONAL).strip() or _SCOPE_PERSONAL
    if scope not in (_SCOPE_PERSONAL, _SCOPE_SHARED):
        raise HTTPException(400, f"Invalid scope: {scope!r}")
    try:
        project_dir = _create_project(payload.name, scope)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return JSONResponse({"name": project_dir.name, "path": str(project_dir), "scope": scope})


def _writable_project_dir(name: str) -> Path:
    project_dir = _get_project_dir(name)
    if project_dir is None:
        raise HTTPException(404, f"Project not found: {name}")
    (project_dir / "download").mkdir(parents=True, exist_ok=True)
    return project_dir


@app.get("/api/projects/{name}/inputs")
def api_project_inputs(name: str):
    project_dir = _get_project_dir(name)
    if project_dir is None:
        raise HTTPException(404, f"Project not found: {name}")
    download_dir = project_dir / "download"
    files: List[Dict] = []
    total = 0
    if download_dir.is_dir():
        for p in sorted(download_dir.iterdir()):
            if not p.is_file() or p.name.startswith("."):
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            files.append({"name": p.name, "size": st.st_size, "mtime": st.st_mtime})
            total += st.st_size
    return JSONResponse({"files": files, "total_bytes": total, "count": len(files)})


@app.delete("/api/projects/{name}/inputs/{filename}")
def api_project_input_delete(name: str, filename: str):
    if not filename or "/" in filename or "\\" in filename or filename.startswith(".") or ".." in filename:
        raise HTTPException(400, "Invalid filename")
    project_dir = _get_project_dir(name)
    if project_dir is None:
        raise HTTPException(404, f"Project not found: {name}")
    target = project_dir / "download" / filename
    if not target.is_file() and not target.is_symlink():
        raise HTTPException(404, f"File not found: {filename}")
    target.unlink()
    return JSONResponse({"deleted": filename})


@app.post("/api/projects/{name}/upload")
async def api_project_upload(name: str, files: List[UploadFile] = File(...)):
    project_dir = _writable_project_dir(name)
    download_dir = project_dir / "download"
    saved = 0
    for f in files:
        if not f.filename:
            continue
        target = download_dir / Path(f.filename).name
        async with aiofiles.open(target, "wb") as out:
            while True:
                chunk = await f.read(1024 * 1024)
                if not chunk:
                    break
                await out.write(chunk)
        saved += 1
    return JSONResponse({"uploaded": saved})


class LinkLocalRequest(BaseModel):
    path: str


@app.post("/api/projects/{name}/link-local")
def api_project_link_local(name: str, payload: LinkLocalRequest):
    project_dir = _writable_project_dir(name)
    src = Path((payload.path or "").strip()).expanduser()
    if not src.exists():
        raise HTTPException(400, f"Input path not found: {src}")
    download_dir = project_dir / "download"
    _accept = (".fastq.gz",)
    candidates = [src] if src.is_file() else sorted(
        f for f in src.iterdir() if f.is_file() and f.name.lower().endswith(_accept))
    count = 0
    for f in candidates:
        if not f.name.lower().endswith(_accept):
            continue
        target = download_dir / f.name
        if not target.exists():
            target.symlink_to(f.resolve())
            count += 1
    return JSONResponse({"linked": count})


class SraRequest(BaseModel):
    accessions: List[str]
    folder: Optional[str] = None


@app.post("/api/projects/{name}/sra/download")
def api_project_sra_download(name: str, payload: SraRequest):
    project_dir = _writable_project_dir(name)
    try:
        expanded, mapping = expand_accessions_with_mapping(payload.accessions, strict=True)
    except SRAExpansionError as e:
        raise HTTPException(
            502,
            f"Could not resolve SRA accessions via NCBI eutils: {e}. "
            "This is usually NCBI rate-limiting; wait ~30 s and retry.")
    download_root = project_dir / "download"
    if payload.folder:
        download_root = download_root / Path(payload.folder).name
    download_root.mkdir(parents=True, exist_ok=True)
    try:
        write_crosswalk_tsv(download_root, mapping)
    except OSError as e:
        logger.warning("Failed to write sra_crosswalk.tsv: %s", e)
    script = build_download_script(download_root, expanded, allow_insecure_https=False)
    script_path = download_root / "download_sra.sh"
    script_path.write_text(script, encoding="utf-8")
    script_path.chmod(0o755)
    env = {"PATH": os.environ.get("PATH", "")}
    job_id = job_manager.start_job(
        name=f"sra_download — {name}",
        command=["bash", str(script_path)], cwd=download_root, env=env)
    return JSONResponse({"job_id": job_id})


@app.get("/api/projects/{name}/sra-crosswalk")
def api_project_sra_crosswalk(name: str):
    project_dir = _get_project_dir(name)
    if project_dir is None:
        raise HTTPException(404, f"Project not found: {name}")
    crosswalk = project_dir / "download" / "sra_crosswalk.tsv"
    if not crosswalk.is_file():
        raise HTTPException(404, "No SRA crosswalk for this project")
    return FileResponse(crosswalk, media_type="text/plain")


@app.get("/api/projects/{name}/samples")
def api_project_samples(name: str):
    project_dir = _get_project_dir(name)
    if project_dir is None:
        raise HTTPException(404, f"Project not found: {name}")
    download_dir = project_dir / "download"
    if not download_dir.is_dir():
        return JSONResponse([])
    return JSONResponse(_list_fastq_pairs(download_dir))


# ---------------------------------------------------------------------------
# Per-sample submission metadata (web entry + Excel).
#
# Canonical store: <project>/irma/sample_metadata.json (edited by the web UI).
# Mirror workbook: <project>/irma/sample_metadata.xlsx (Download / Replace).
# Both are matched on the sample name; the pipeline reads them to build the
# submission FASTA headers.
# ---------------------------------------------------------------------------
def _irma_dir(name: str) -> Path:
    project_dir = _get_project_dir(name)
    if project_dir is None:
        raise HTTPException(404, f"Project not found: {name}")
    d = project_dir / _TOOL_SUBDIR
    d.mkdir(parents=True, exist_ok=True)
    return d


@app.get("/api/projects/{name}/metadata")
def api_get_metadata(name: str):
    project_dir = _get_project_dir(name)
    if project_dir is None:
        raise HTTPException(404, f"Project not found: {name}")
    irma_dir = project_dir / _TOOL_SUBDIR
    records = meta_mod.load_json(irma_dir) if irma_dir.is_dir() else {}
    # Seed empty rows for samples that exist but have no metadata yet.
    samples = [p["sample"] for p in _list_fastq_pairs(project_dir / "download")] \
        if (project_dir / "download").is_dir() else []
    for s in samples:
        if s not in records:
            blank = {f: "" for f in meta_mod.FIELDS}
            blank["sample"] = s
            blank["organism"] = meta_mod.DEFAULT_ORGANISM
            records[s] = blank
    return JSONResponse({
        "fields": meta_mod.FIELDS,
        "samples": samples,
        "records": records,
        "xlsx_present": (irma_dir / meta_mod.METADATA_XLSX).is_file(),
    })


class MetadataPayload(BaseModel):
    records: Dict[str, Dict[str, Any]]


@app.post("/api/projects/{name}/metadata")
def api_save_metadata(name: str, payload: MetadataPayload):
    irma_dir = _irma_dir(name)
    saved = meta_mod.save_json(irma_dir, payload.records or {})
    # Regenerate the mirror workbook so Download reflects the latest web edits.
    try:
        meta_mod.write_xlsx(meta_mod.load_json(irma_dir), irma_dir / meta_mod.METADATA_XLSX)
    except Exception as exc:  # noqa: BLE001
        logger.warning("metadata xlsx regen failed: %s", exc)
    return JSONResponse({"ok": True, "json": str(saved), "count": len(payload.records or {})})


@app.get("/api/projects/{name}/metadata.xlsx")
def api_download_metadata_xlsx(name: str):
    irma_dir = _irma_dir(name)
    xlsx = irma_dir / meta_mod.METADATA_XLSX
    if not xlsx.is_file():
        meta_mod.write_xlsx(meta_mod.load_json(irma_dir), xlsx)
    return FileResponse(
        xlsx,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{name}_sample_metadata.xlsx"'},
    )


@app.post("/api/projects/{name}/metadata/upload")
async def api_upload_metadata_xlsx(name: str, file: UploadFile = File(...),
                                   key_col: Optional[str] = Query(None)):
    """Replace the project metadata from an uploaded Excel, matched on sample."""
    irma_dir = _irma_dir(name)
    dest = irma_dir / meta_mod.METADATA_XLSX
    async with aiofiles.open(dest, "wb") as out:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            await out.write(chunk)
    try:
        table = meta_mod.read_xlsx(dest, key_col=key_col)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"Could not parse the uploaded Excel: {exc}")
    if not table:
        raise HTTPException(
            400, "No sample rows found. The sheet needs a 'sample' (or accession) "
                 "column plus host/state/collection_year columns.")
    meta_mod.save_json(irma_dir, table)
    return JSONResponse({"imported": len(table), "records": meta_mod.load_json(irma_dir)})


# ---------------------------------------------------------------------------
# Per-sample IRMA results (read straight from <project>/irma/<sample>/).
# ---------------------------------------------------------------------------
def _collect_result_files(run_dir: Path, include_all: bool) -> List[Dict]:
    files: List[Dict] = []
    if not run_dir.is_dir():
        return files
    for p in sorted(run_dir.rglob("*")):
        if not p.is_file() or p.name.endswith(".log"):
            continue
        rel = str(p.relative_to(run_dir))
        category = _result_category(rel)
        if not include_all and category is None:
            continue
        stat = p.stat()
        files.append({
            "name": rel, "path": str(p),
            "label": _result_label(rel, category),
            "size": stat.st_size, "mtime": stat.st_mtime,
            "openable": _can_open_inline(rel), "category": category,
        })

    def sort_key(f):
        category = f.get("category")
        if category in _CATEGORY_ORDER:
            return (_CATEGORY_ORDER[category], f["name"])
        return (50, f["name"])

    files.sort(key=sort_key)
    for f in files:
        f.pop("mtime", None)
        if include_all and f.get("category") is None:
            f["label"] = f["name"]
    return files


def _sample_run_status(run_dir: Path) -> str:
    run_dir_str = str(run_dir)
    for job in job_manager.list_jobs():
        if job.get("cwd") == run_dir_str and job.get("status") == "running":
            return "running"
    try:
        if run_dir.is_dir() and any(p.is_file() for p in run_dir.rglob("*")):
            return "done"
    except PermissionError:
        pass
    return "none"


@app.get("/api/projects/{name}/samples/{sample}/irma-results")
def api_sample_irma_results(name: str, sample: str, all: int = Query(0)):
    project_dir = _get_project_dir(name)
    if project_dir is None:
        raise HTTPException(404, f"Project not found: {name}")
    run_dir = project_dir / _TOOL_SUBDIR / sample
    return JSONResponse({
        "project": name, "sample": sample,
        "present": run_dir.is_dir(),
        "status": _sample_run_status(run_dir),
        "run_dir": str(run_dir),
        "files": _collect_result_files(run_dir, bool(all)),
    })


@app.get("/api/projects/{name}/samples/{sample}/irma-table")
def api_sample_irma_table(name: str, sample: str):
    """Parsed per-sample summary: assembly_stats + GenoFLU + HA cleavage +
    provenance, so the Results pane renders everything in one fetch."""
    project_dir = _get_project_dir(name)
    if project_dir is None:
        raise HTTPException(404, f"Project not found: {name}")
    run_dir = project_dir / _TOOL_SUBDIR / sample

    def _load(p: Path) -> Dict[str, Any]:
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    return JSONResponse({
        "project": name, "sample": sample,
        "present": run_dir.is_dir(),
        "assembly": _load(run_dir / "assembly_stats.json"),
        "genoflu": _load(run_dir / "genoflu_result.json"),
        "ha_cleavage": _load(run_dir / "ha_cleavage.json"),
        "fastq_qc": _load(run_dir / "fastq_qc.json"),
        "provenance": _load(run_dir / "run_manifest.json"),
    })


@app.get("/api/projects/{name}/runs")
def api_project_runs(name: str):
    """Summary of every IRMA run in a project — one row per sample that has a
    run dir. Powers the 'Current run' results pane (searchable, date-filterable)
    so users don't have to reopen Projects to see what ran."""
    project_dir = _get_project_dir(name)
    if project_dir is None:
        raise HTTPException(404, f"Project not found: {name}")
    runs_root = project_dir / _TOOL_SUBDIR
    rows: List[Dict[str, Any]] = []
    if runs_root.is_dir():
        for run_dir in sorted(runs_root.iterdir(), key=_safe_mtime, reverse=True):
            if not run_dir.is_dir():
                continue
            asm = {}
            try:
                asm = json.loads((run_dir / "assembly_stats.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                asm = {}
            geno = {}
            try:
                geno = json.loads((run_dir / "genoflu_result.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                geno = {}
            rows.append({
                "sample": run_dir.name,
                "status": _sample_run_status(run_dir),
                "mtime": _safe_mtime(run_dir),
                "subtype": asm.get("subtype") or "",
                "segment_count": asm.get("segment_count") or 0,
                "verdict": (asm.get("overall_verdict") or "").upper(),
                "genotype": geno.get("genotype") or "",
                "has_report": (run_dir / "report.html").is_file() or (run_dir / "report.pdf").is_file(),
            })
    return JSONResponse({"project": name, "runs": rows})


@app.get("/api/projects/{name}/file")
def api_project_file(name: str, path: str = Query(...), inline: int = 0):
    project_dir = _get_project_dir(name)
    if project_dir is None:
        raise HTTPException(404, f"Project not found: {name}")
    root = project_dir.resolve()
    target = Path(path).resolve()
    if root != target and root not in target.parents:
        raise HTTPException(403, "Path outside project directory")
    if not target.is_file():
        raise HTTPException(404, f"File not found: {path}")
    media_type = _media_type_for(target.name)
    want_inline = bool(inline) and _can_open_inline(target.name)
    disposition = "inline" if want_inline else "attachment"
    return FileResponse(target, media_type=media_type,
                        headers={"Content-Disposition": f'{disposition}; filename="{target.name}"'})


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def _resolve_app_version() -> str:
    """Version of the deployed checkout — the exact string the Diagnostic
    Tools Dashboard shows for this tool (`git describe --tags --always`,
    the same command bdtools runs). Resolved once at startup; empty when
    git or the .git dir is unavailable, in which case the frontend falls
    back to its built-in constant."""
    try:
        out = subprocess.run(
            ["git", "-C", str(_REPO_ROOT), "describe", "--tags", "--always"],
            capture_output=True, text=True, timeout=10,
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


APP_VERSION = _resolve_app_version()


@app.get("/api/config")
def api_get_config():
    cfg = load_config()
    # Deployed checkout's version (git describe) — what the dashboard shows.
    cfg["app_version"] = APP_VERSION
    return JSONResponse(cfg)


class ConfigPayload(BaseModel):
    irma_module: Optional[str] = None
    run_genoflu: Optional[bool] = None
    genoflu_db: Optional[str] = None
    genoflu_pident: Optional[float] = None
    projects_root: Optional[str] = None
    shared_projects_root: Optional[str] = None
    saved_project_roots: Optional[List[str]] = None


@app.post("/api/config")
def api_save_config(payload: ConfigPayload):
    cfg = load_config()
    updates = payload.model_dump(exclude_none=True)
    cfg.update(updates)
    roots = cfg.get("saved_project_roots") or []
    if isinstance(roots, list):
        seen, cleaned = set(), []
        for r in roots:
            r = (r or "").strip()
            if r and r not in seen:
                seen.add(r); cleaned.append(r)
        cfg["saved_project_roots"] = cleaned
    save_config(cfg)
    return JSONResponse({"ok": True})


@app.get("/api/browse-dirs")
def api_browse_dirs(path: str = ""):
    try:
        p = (Path(path).expanduser() if path.strip() else Path.home()).resolve()
    except (OSError, RuntimeError):
        raise HTTPException(400, "Invalid path")
    if not p.is_dir():
        raise HTTPException(400, f"Not a directory: {p}")
    entries: List[Dict[str, str]] = []
    try:
        for child in sorted(p.iterdir(), key=lambda c: c.name.lower()):
            if child.name.startswith("."):
                continue
            try:
                if child.is_dir():
                    entries.append({"name": child.name, "path": str(child)})
            except OSError:
                continue
    except PermissionError:
        raise HTTPException(403, f"Permission denied: {p}")
    parent = str(p.parent) if p.parent != p else None
    return JSONResponse({"path": str(p), "parent": parent, "entries": entries})


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
class RunPayload(BaseModel):
    project: str
    r1: str
    r2: Optional[str] = None
    module: Optional[str] = None             # FLU | CoV
    run_genoflu: Optional[bool] = None
    genoflu_pident: Optional[float] = None
    genoflu_db: Optional[str] = None
    metadata_xlsx: Optional[str] = None      # external Excel (optional override)
    metadata_key_col: Optional[str] = None
    header_style: Optional[str] = None       # ncbi | strain


@app.post("/api/run")
def api_run(payload: RunPayload):
    cfg = load_config()
    module = (payload.module or cfg.get("irma_module", "FLU")).strip() or "FLU"
    run_genoflu = payload.run_genoflu if payload.run_genoflu is not None else cfg.get("run_genoflu", True)
    genoflu_db = payload.genoflu_db or cfg.get("genoflu_db", "")
    genoflu_pident = payload.genoflu_pident if payload.genoflu_pident is not None \
        else cfg.get("genoflu_pident", 98.0)

    project_dir = _get_project_dir(payload.project)
    if project_dir is None:
        raise HTTPException(404, f"Project not found: {payload.project}")

    primary = Path(payload.r1)
    if not primary.exists():
        raise HTTPException(400, f"Input R1 not found: {payload.r1}")
    sample_name, _ = _strip_read_tag(primary.name)

    run_dir = project_dir / _TOOL_SUBDIR / sample_name
    for existing in job_manager.list_jobs():
        if existing.get("status") == "running" and existing.get("cwd") == str(run_dir):
            raise HTTPException(
                409, f"A run is already in progress for {sample_name} "
                     f"(job {existing['id'][:8]}). Wait for it to finish before re-running.")
    run_dir.mkdir(parents=True, exist_ok=True)

    script = _BIN_DIR / "irma_pipeline.py"
    command = [sys.executable, "-u", str(script),
               "--sample", sample_name, "--outdir", str(run_dir),
               "-r1", str(primary), "--module", module]
    if payload.r2:
        r2 = Path(payload.r2)
        if not r2.exists():
            raise HTTPException(400, f"R2 file not found: {payload.r2}")
        command.extend(["-r2", str(r2)])
    if not run_genoflu:
        command.append("--no-genoflu")
    if genoflu_db:
        command.extend(["--genoflu-db", genoflu_db])
    if genoflu_pident is not None:
        command.extend(["--genoflu-pident", str(genoflu_pident)])
    if payload.metadata_xlsx:
        command.extend(["--metadata-xlsx", payload.metadata_xlsx])
        if payload.metadata_key_col:
            command.extend(["--metadata-key-col", payload.metadata_key_col])
    if payload.header_style:
        command.extend(["--header-style", payload.header_style])

    env = {
        "PYTHONPATH": str(_BIN_DIR),
        "PATH": os.environ.get("PATH", ""),
        "PYTHONUNBUFFERED": "1",
        "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
    }
    job_name = f"{payload.project}/{sample_name} — IRMA {module}"
    job_id = job_manager.start_job(name=job_name, command=command, cwd=run_dir, env=env)
    return JSONResponse({"job_id": job_id, "run_dir": str(run_dir)})


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------
@app.get("/api/jobs")
def api_list_jobs():
    return JSONResponse(job_manager.list_jobs())


@app.get("/api/jobs/{job_id}")
def api_get_job(job_id: str):
    job = job_manager.get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    return JSONResponse(job)


@app.get("/api/jobs/{job_id}/log")
async def api_job_log(job_id: str, request: Request):
    job = job_manager.get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    log_path = Path(job["log_path"])
    _ansi_re = re.compile(r'\x1b\[[0-9;]*[mGKHFABCDJsur]')

    async def event_stream():
        position = 0
        while True:
            if await request.is_disconnected():
                break
            current_job = job_manager.get_job(job_id)
            if log_path.exists():
                async with aiofiles.open(log_path, "r", encoding="utf-8", errors="replace") as f:
                    await f.seek(position)
                    chunk = await f.read(4096)
                    if chunk:
                        for line in chunk.splitlines(keepends=True):
                            clean = _ansi_re.sub("", line.rstrip())
                            if clean:
                                yield f"data: {clean}\n\n"
                        position += len(chunk.encode("utf-8"))
            if current_job and current_job["status"] in ("succeeded", "failed"):
                yield "data: [DONE]\n\n"
                break
            await asyncio.sleep(0.5)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# Result file categorization / media
# ---------------------------------------------------------------------------
_INLINE_MEDIA = {
    ".pdf": "application/pdf", ".html": "text/html", ".htm": "text/html",
    ".txt": "text/plain", ".log": "text/plain", ".json": "application/json",
    ".tsv": "text/plain", ".png": "image/png", ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg", ".svg": "image/svg+xml", ".csv": "text/plain",
}
_DOWNLOAD_MEDIA = {
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls": "application/vnd.ms-excel", ".vcf": "text/plain",
    ".fasta": "text/plain", ".fa": "text/plain", ".fna": "text/plain",
    ".gz": "application/gzip",
}


def _can_open_inline(name: str) -> bool:
    return Path(name).suffix.lower() in _INLINE_MEDIA


def _media_type_for(name: str) -> str:
    ext = Path(name).suffix.lower()
    return _INLINE_MEDIA.get(ext) or _DOWNLOAD_MEDIA.get(ext) or "application/octet-stream"


def _result_category(rel: str) -> Optional[str]:
    path = Path(rel)
    name = path.name
    parts = path.parts
    if any(part.startswith(".") for part in parts):
        return None
    if name.endswith(".fastq.gz"):
        return None
    if name == "report.html":
        return "report_html"
    if name == "report.pdf":
        return "report_pdf"
    if name.endswith("_stats.xlsx"):
        return "stats_xlsx"
    if name.endswith("-submission.fasta"):
        return "submission_fasta"
    if name == "assembly.fasta":
        return "assembly_fasta"
    if name == "genoflu_genotype.xlsx":
        return "genoflu_table_xlsx"
    if name in ("genoflu_genotype.tsv", "genoflu_genotype.txt"):
        return "genoflu_table_tsv"
    if name == "genoflu_result.json":
        return "genoflu_json"
    if name == "assembly_stats.json":
        return "assembly_stats"
    if name == "fastq_qc.json":
        return "fastq_qc"
    if name == "ha_cleavage.json":
        return "ha_cleavage"
    if name == "run_manifest.json":
        return "run_manifest"
    if name.endswith("-coverageDiagram.pdf") or name == "READ_PERCENTAGES.pdf":
        return "coverage_figure"
    if name == "pipeline.log":
        return "log"
    return None


_CATEGORY_ORDER = {
    "report_html": 0, "report_pdf": 1, "stats_xlsx": 2, "submission_fasta": 3,
    "assembly_fasta": 4, "genoflu_table_xlsx": 5, "genoflu_table_tsv": 6,
    "genoflu_json": 7, "assembly_stats": 8, "fastq_qc": 9, "ha_cleavage": 10,
    "coverage_figure": 11, "run_manifest": 12, "log": 99,
}


def _coverage_figure_label(name: str) -> str:
    """Per-figure label for IRMA's coverage outputs so the Results list
    distinguishes segments instead of repeating "IRMA coverage figure".

    IRMA names each diagram after its reference (e.g. A_HA_H5-coverageDiagram.pdf,
    A_PB2-coverageDiagram.pdf); READ_PERCENTAGES.pdf is the per-segment read share.
    """
    if name == "READ_PERCENTAGES.pdf":
        return "IRMA read percentages (all segments)"
    ref = name[:-len("-coverageDiagram.pdf")] if name.endswith("-coverageDiagram.pdf") else name
    parts = ref.split("_")
    gene = parts[1].upper() if len(parts) >= 2 and parts[1] else ref.upper()
    segnum = meta_mod.SEGMENT_NUMBER.get(gene)
    if segnum:
        return f"IRMA coverage — segment {segnum} {gene} ({ref})"
    return f"IRMA coverage — {ref}"


def _result_label(rel: str, category: Optional[str]) -> str:
    if category == "coverage_figure":
        return _coverage_figure_label(Path(rel).name)
    return {
        "report_html": "Report (interactive HTML — coverage & SNP charts)",
        "report_pdf": "Report (PDF)",
        "stats_xlsx": "IRMA assembly & QC statistics (Excel workbook)",
        "submission_fasta": "Submission FASTA (metadata headers)",
        "assembly_fasta": "Assembly FASTA (raw IRMA consensus)",
        "genoflu_table_xlsx": "GenoFLU genotype — per-segment lineages (Excel workbook)",
        "genoflu_table_tsv": "GenoFLU genotype — per-segment lineages (tab-delimited text)",
        "genoflu_json": "GenoFLU result (JSON)",
        "assembly_stats": "Assembly + coverage stats (JSON)",
        "fastq_qc": "Input read QC (JSON)",
        "ha_cleavage": "HA cleavage site (JSON)",
        "coverage_figure": "IRMA coverage figure",
        "run_manifest": "Run manifest / provenance (JSON)",
        "log": "Pipeline log",
    }.get(category, rel)


@app.get("/api/jobs/{job_id}/results")
def api_job_results(job_id: str, all: int = Query(0)):
    job = job_manager.get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    files = []
    cwd = job.get("cwd")
    if cwd and Path(cwd).is_dir():
        run_dir = Path(cwd)
        for p in sorted(run_dir.rglob("*")):
            if p.is_file() and not p.name.endswith(".log"):
                rel = str(p.relative_to(run_dir))
                category = _result_category(rel)
                if not all and category is None:
                    continue
                files.append({
                    "name": rel, "label": _result_label(rel, category),
                    "size": p.stat().st_size, "mtime": p.stat().st_mtime,
                    "openable": _can_open_inline(rel), "category": category,
                })
    log_path = Path(job.get("log_path", ""))
    if log_path.is_file():
        files.append({
            "name": "pipeline_log.txt", "label": "Pipeline log",
            "size": log_path.stat().st_size, "mtime": log_path.stat().st_mtime,
            "openable": True, "category": "log", "is_log": True,
        })

    def sort_key(f):
        if f.get("is_log"):
            return (_CATEGORY_ORDER["log"], f["name"])
        category = f.get("category")
        if category in _CATEGORY_ORDER:
            return (_CATEGORY_ORDER[category], f["name"])
        return (50, f["name"])

    files.sort(key=sort_key)
    for file in files:
        file.pop("mtime", None)
        if all and file.get("category") is None:
            file["label"] = file["name"]
    return JSONResponse(files)


@app.get("/api/jobs/{job_id}/file")
def api_job_file(job_id: str, path: str = Query(...), inline: int = 0):
    job = job_manager.get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    if path == "pipeline_log.txt":
        target = Path(job.get("log_path", ""))
        display_name = f"{job_id[:8]}_pipeline_log.txt"
    else:
        cwd = job.get("cwd")
        if not cwd:
            raise HTTPException(404, "No run directory for job")
        run_dir = Path(cwd).resolve()
        target = (run_dir / path).resolve()
        if run_dir != target and run_dir not in target.parents:
            raise HTTPException(403, "Path outside run directory")
        display_name = target.name
    if not target.is_file():
        raise HTTPException(404, f"File not found: {path}")
    media_type = _media_type_for(target.name)
    want_inline = bool(inline) and _can_open_inline(target.name)
    disposition = "inline" if want_inline else "attachment"
    return FileResponse(target, media_type=media_type,
                        headers={"Content-Disposition": f'{disposition}; filename="{display_name}"'})


# ---------------------------------------------------------------------------
# Static frontend — must be last
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# The Results pane — every completed sample in one searchable table.
#
# Modelled on vSNP's Step 1 Results. The per-sample endpoints elsewhere in
# this file answer "tell me about THIS one", which is why results were only ever
# visible by expanding a row in the Projects tree; there was no way to see, sort,
# search or export everything a project had produced. This answers that.
# ---------------------------------------------------------------------------
_RP_SUBDIR = _TOOL_SUBDIR

def _rp_finished_at(run_dir: Path) -> str:
    """When this run finished, as an ISO string ("" if unknown).

    Prefer the pipeline's own record over filesystem mtimes: a later re-read or
    an rsync can touch files long after the analysis actually ran."""
    try:
        data = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
        for key in ("pipeline_finished_at", "finished_at_utc", "finished_at", "timestamp"):
            val = str(data.get(key) or "").strip()
            if val:
                return val
    except (OSError, ValueError, AttributeError):
        pass
    try:
        newest = max((p.stat().st_mtime for p in run_dir.rglob("*") if p.is_file()), default=0)
        if newest:
            from datetime import datetime, timezone
            return datetime.fromtimestamp(newest, tz=timezone.utc).isoformat()
    except (OSError, ValueError):
        pass
    return ""


def _rp_flags(run_dir: Path) -> Dict[str, Any]:
    """pass / review / fail plus reasons.

    The run's exit status outranks any QC verdict: qc.json grades the INPUT, so a
    run whose analysis step exited non-zero — producing nothing — would otherwise
    still be reported as "pass". A Results pane that says PASS for a failed run is
    worse than no pane at all."""
    level, reasons = "pass", []
    try:
        qc = json.loads((run_dir / "qc.json").read_text(encoding="utf-8"))
        verdict = str(qc.get("verdict") or "").strip().lower()
        if verdict in ("fail", "failed"):
            level = "fail"
        elif verdict in ("review", "warn", "warning"):
            level = "review"
        notes = qc.get("notes") or qc.get("reasons") or []
        if isinstance(notes, str):
            notes = [notes]
        reasons = [str(n) for n in notes if str(n).strip()]
    except (OSError, ValueError, AttributeError):
        pass
    try:
        man = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
        rc = man.get("return_code")
        if rc not in (None, 0):
            level = "fail"
            lines = [l.strip() for l in str(man.get("stderr_tail") or "").splitlines() if l.strip()]
            detail = ""
            for i, l in enumerate(lines):
                if l.startswith("*** ERROR"):
                    detail = next((x for x in lines[i + 1:]
                                   if not x.startswith(("Command line:", "Running:"))), "")
                    break
            if not detail:
                detail = next((l for l in lines if "error" in l.lower()), "")
            if not detail:
                detail = next((l for l in reversed(lines)
                               if not l.startswith(("Command line:", "Running:"))), "")
            reasons.insert(0, "the analysis exited %s%s" % (rc, (" — " + detail[:120]) if detail else ""))
    except (OSError, ValueError, AttributeError):
        pass
    return {"level": level, "reasons": reasons}


def _rp_json(run_dir: Path, name: str) -> Dict[str, Any]:
    """Read one of the run's small result JSONs; {} when absent or malformed."""
    try:
        data = json.loads((run_dir / name).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


_rp_status = _sample_run_status


def _rp_metrics(run_dir: Path) -> Dict[str, Any]:
    """Subtype / genotype plus assembled-segment count."""
    geno = _rp_json(run_dir, "genoflu_result.json")
    stats = _rp_json(run_dir, "assembly_stats.json")
    # `segments` is a per-gene list of dicts; the count lives in segment_count.
    # Returning the list would dump a screenful of JSON into a table cell.
    segments = stats.get("segment_count")
    if segments is None:
        seglist = stats.get("segments")
        segments = len(seglist) if isinstance(seglist, list) else None
    verdict = str(stats.get("overall_verdict") or "").strip() or None
    return {
        "subtype": stats.get("subtype") or geno.get("genotype") or None,
        "segments": segments,
        "genotype": geno.get("genotype") or None,
        "verdict": verdict,
    }


_RP_CROSS_PROBES = [
    ("kraken", "krona", "\U0001F4CA Krona", "kraken", "*_krona.html",
     "./api/projects/{p}/kraken/samples/{s}/krona"),
]


def _rp_cross_tool(project: str, project_dir: Path, sample: str) -> List[Dict]:
    """Sibling tools' outputs for the same sample.

    Every tool builds the same project skeleton, so a sample analysed here may
    also have a Kraken run. Those files live outside this tool's run dir, which is
    exactly why they never appeared in a results view."""
    out: List[Dict] = []
    for tool, kind, label, subdir, glob, href in _RP_CROSS_PROBES:
        base = project_dir / subdir
        if not base.is_dir():
            continue
        d = base / sample
        if not d.is_dir():
            try:
                cands = sorted(x for x in base.iterdir()
                               if x.is_dir() and x.name.startswith(sample + "_"))
            except OSError:
                cands = []
            d = cands[0] if cands else None
        if d is None:
            continue
        try:
            if not any(d.rglob(glob)):
                continue
        except OSError:
            continue
        out.append({"tool": tool, "kind": kind, "label": label,
                    "href": href.format(p=project, s=sample)})
    return out


def _rp_rows(name: str, include_all: bool = False) -> List[Dict]:
    project_dir = _get_project_dir(name)
    if project_dir is None:
        raise HTTPException(404, "Project not found: %s" % name)
    root = project_dir / _RP_SUBDIR
    rows: List[Dict] = []
    if not root.is_dir():
        return rows
    try:
        entries = sorted(p for p in root.iterdir() if p.is_dir())
    except (OSError, PermissionError):
        return rows
    for d in entries:
        rows.append({
            "sample": d.name,
            "status": _rp_status(d),
            "run_date": _rp_finished_at(d),
            "run_dir": str(d),
            "flags": _rp_flags(d),
            "metrics": _rp_metrics(d),
            "files": _collect_result_files(d, include_all),
            "cross_tool": _rp_cross_tool(name, project_dir, d.name),
        })
    rows.sort(key=lambda r: (r["run_date"] or "", r["sample"]), reverse=True)
    return rows


def _rp_filter(rows: List[Dict], start: str, end: str, q: str) -> List[Dict]:
    """Apply the pane's filters server-side too, so an export matches the view."""
    ql = (q or "").strip().lower()
    out = []
    for r in rows:
        if ql and ql not in r["sample"].lower():
            continue
        day = (r.get("run_date") or "")[:10]
        if start and (not day or day < start):
            continue
        if end and (not day or day > end):
            continue
        out.append(r)
    return out


@app.get("/api/projects/{name}/results")
def api_project_results(name: str, all: int = Query(0)):
    return JSONResponse({"project": name, "tool": _RP_SUBDIR,
                         "rows": _rp_rows(name, include_all=bool(all))})


_RP_EXPORT_COLUMNS = [("sample", "Sample"), ("status", "Status"),
                      ("run_date", "Run date"), ("qc", "QC"),
                      ("qc_reasons", "QC notes")] + [("subtype", "Subtype"), ("segments", "Segments"), ("genotype", "Genotype"), ("verdict", "Verdict")] + \
                     [("run_dir", "Run directory")]


def _rp_export_records(name, start, end, q):
    for r in _rp_filter(_rp_rows(name), start, end, q):
        m = r.get("metrics") or {}
        rec = {"sample": r["sample"], "status": r["status"],
               "run_date": (r.get("run_date") or "")[:19],
               "qc": (r.get("flags") or {}).get("level", ""),
               "qc_reasons": "; ".join((r.get("flags") or {}).get("reasons", [])),
               "run_dir": r.get("run_dir", "")}
        for key, _label in _RP_EXPORT_COLUMNS:
            rec.setdefault(key, m.get(key))
        yield rec


@app.get("/api/projects/{name}/results.csv")
def api_project_results_csv(name: str, start: str = Query(""), end: str = Query(""),
                            q: str = Query("")):
    import csv
    import io
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=[k for k, _ in _RP_EXPORT_COLUMNS], extrasaction="ignore")
    w.writerow({k: label for k, label in _RP_EXPORT_COLUMNS})
    for rec in _rp_export_records(name, start, end, q):
        w.writerow(rec)
    return Response(content=buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition":
                             'attachment; filename="%s_irma_results.csv"' % name})


@app.get("/api/projects/{name}/results.xlsx")
def api_project_results_xlsx(name: str, start: str = Query(""), end: str = Query(""),
                             q: str = Query("")):
    try:
        from openpyxl import Workbook
    except ImportError:
        raise HTTPException(501, "Excel export needs openpyxl in this tool's environment.")
    import io
    wb = Workbook()
    ws = wb.active
    ws.title = "Results"
    ws.append([label for _, label in _RP_EXPORT_COLUMNS])
    for rec in _rp_export_records(name, start, end, q):
        ws.append([rec.get(k) for k, _ in _RP_EXPORT_COLUMNS])
    for i, (_k, label) in enumerate(_RP_EXPORT_COLUMNS, start=1):
        ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = max(12, len(label) + 2)
    ws.freeze_panes = "A2"
    buf = io.BytesIO()
    wb.save(buf)
    return Response(content=buf.getvalue(),
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition":
                             'attachment; filename="%s_irma_results.xlsx"' % name})


if _FRONTEND_DIST.is_dir():
    _INDEX_HTML = _FRONTEND_DIST / "index.html"

    @app.get("/")
    def index():
        return FileResponse(_INDEX_HTML,
                            headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

    app.mount("/", StaticFiles(directory=str(_FRONTEND_DIST), html=True), name="static")
else:
    @app.get("/")
    def root():
        return JSONResponse(
            {"error": "Frontend not built. Run: cd frontend && npm run build"},
            status_code=503)
