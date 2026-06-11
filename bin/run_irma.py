#!/usr/bin/env python
"""
run_irma.py — run CDC IRMA on a read set, capturing EVERY option used and
writing ISO-aware provenance.

IRMA (Iterative Refinement Meta-Assembler, https://github.com/CDCgov/irma)
assembles segmented RNA-virus genomes (influenza A/B, SARS-CoV-2) directly from
reads with iterative read gathering + variant calling. Surveillance assemblies
must be defensible, so this wrapper records the module, every command-line
argument, the IRMA version, paired/single mode, and the tool versions used, then
never raises — a non-zero IRMA exit is recorded, not fatal.

IRMA writes its output into a directory named by the run label inside the CWD;
we run it in `outdir` with label `irma`, so results land in `<outdir>/irma/`
(amended_consensus/, tables/<seg>-coverage.txt, tables/<seg>-variants.txt,
figures/). The orchestrator (irma_pipeline.py) reads those for QC and reporting.

ISO / quality standards referenced in the provenance (for traceability):
  ISO 20397-1:2022 & ISO 20397-2:2021 (massively parallel sequencing — general
  requirements and quality evaluation of sequencing data: the Q20/Q30 read
  metrics and per-segment depth/coverage we report), ISO 15189:2022 (medical
  laboratory quality: traceability, validation, version control, reporting),
  ISO/IEC 17025 (testing-laboratory competence — surveillance / veterinary),
  WOAH Terrestrial Manual 3.3.4 (avian influenza — reference standard for
  detection & characterization), WHO/WOAH/FAO H5 clade nomenclature (2.3.4.4b).

Run standalone:
  python run_irma.py -r1 R1.fastq.gz [-r2 R2.fastq.gz] --outdir DIR \
      --name SAMPLE [--module FLU]
"""

import argparse
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ISO_REFERENCES = [
    {"standard": "ISO 20397-1:2022", "scope": "Massively parallel sequencing — general requirements"},
    {"standard": "ISO 20397-2:2021", "scope": "Massively parallel sequencing — quality evaluation of sequencing data (Q20/Q30, depth, coverage)"},
    {"standard": "ISO 15189:2022", "scope": "Medical laboratory quality & competence (traceability, validation, version control, reporting)"},
    {"standard": "ISO/IEC 17025", "scope": "Testing-laboratory competence (surveillance / veterinary diagnostics)"},
    {"standard": "WOAH Terrestrial Manual 3.3.4", "scope": "Avian influenza — reference standard for detection & characterization"},
    {"standard": "WHO/WOAH/FAO H5 nomenclature", "scope": "Goose/Guangdong H5 clade naming (2.3.4.4b)"},
]

VALID_MODULES = {"FLU", "FLU-minion", "CoV", "CoV-minion", "FLU-pacbio"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(msg: str) -> None:
    print(msg, flush=True)


def _tool_version(cmd: List[str]) -> Optional[str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        out = (proc.stdout or "").strip() or (proc.stderr or "").strip()
        return out.splitlines()[0].strip() if out else None
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return None


def _irma_version() -> Optional[str]:
    """IRMA prints its version/banner to stdout when run with no args."""
    try:
        proc = subprocess.run(["IRMA"], capture_output=True, text=True, timeout=60)
        out = (proc.stdout or "") + (proc.stderr or "")
        for ln in out.splitlines():
            if "version" in ln.lower() or ln.strip().lower().startswith("irma"):
                return ln.strip()
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return None
    return None


def run(
    r1: Path,
    outdir: Path,
    name: str,
    r2: Optional[Path] = None,
    module: str = "FLU",
    extra_provenance: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Run IRMA into <outdir>/irma/ and write run_manifest.json. Returns the
    manifest dict (return_code + irma_dir + options)."""
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    module = (module or "FLU").strip()
    if module not in VALID_MODULES:
        log(f"WARNING: unrecognized IRMA module {module!r}; defaulting to FLU.")
        module = "FLU"

    irma_exe = shutil.which("IRMA")
    irma_dir = outdir / "irma"
    # IRMA refuses to overwrite an existing run dir; clear a prior attempt.
    if irma_dir.exists():
        shutil.rmtree(irma_dir, ignore_errors=True)

    paired = bool(r2)
    cmd: List[str] = []
    if irma_exe:
        cmd = ["IRMA", module, str(r1)]
        if r2:
            cmd.append(str(r2))
        cmd.append("irma")  # run label -> <outdir>/irma/
    else:
        log("ERROR: IRMA not found on PATH.")

    env = dict(os.environ)
    env.setdefault("TMPDIR", "/tmp")
    started = _now()
    rc = 127
    stderr_tail = ""
    if cmd:
        log(f"$ {' '.join(cmd)}  (cwd={outdir})")
        try:
            proc = subprocess.run(cmd, cwd=str(outdir), env=env,
                                  capture_output=True, text=True)
            rc = proc.returncode
            if proc.stdout:
                log(proc.stdout)
            if proc.stderr:
                print(proc.stderr, file=sys.stderr, flush=True)
                stderr_tail = "\n".join(proc.stderr.splitlines()[-25:])
        except (FileNotFoundError, OSError) as exc:
            rc = 127
            stderr_tail = f"IRMA failed to launch: {exc}"
            log(f"ERROR: {stderr_tail}")
    else:
        stderr_tail = "IRMA executable not found on PATH"

    finished = _now()
    assembled = irma_dir.is_dir() and any(irma_dir.glob("*.fasta"))
    if assembled:
        log(f"IRMA assembled {len(list(irma_dir.glob('*.fasta')))} segment FASTA(s) in {irma_dir}")
    else:
        log("WARNING: IRMA produced no segment FASTA — assembly likely failed for this sample.")

    manifest: Dict[str, Any] = {
        "tool": "IRMA",
        "sample": name,
        "input_r1": str(r1),
        "input_r2": str(r2) if r2 else None,
        "command": cmd,
        "started_at": started,
        "finished_at": finished,
        "return_code": rc,
        "options": {
            "module": module,
            "paired": paired,
            "run_label": "irma",
            "input_mode": "paired reads" if paired else "single read",
        },
        "outputs": {
            "irma_dir": str(irma_dir),
            "assembled": assembled,
        },
        "versions": {
            "irma": _irma_version(),
            "samtools": _tool_version(["samtools", "--version"]),
            "seqkit": _tool_version(["seqkit", "version"]),
        },
        "reportable_metrics": [
            "per-segment average depth of coverage",
            "per-segment % positions < 10X",
            "per-segment % positions with zero coverage",
            "read Q20 / Q30 (input QC)",
        ],
        "iso_references": ISO_REFERENCES,
        "tmpdir": env.get("TMPDIR"),
        "stderr_tail": stderr_tail,
    }
    if extra_provenance:
        manifest.update(extra_provenance)
    (outdir / "run_manifest.json").write_text(
        __import__("json").dumps(manifest, indent=2) + "\n", encoding="utf-8")
    if rc != 0:
        log(f"WARNING: IRMA exited with code {rc}")
    return manifest


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Run IRMA with full provenance.")
    ap.add_argument("-r1", "--r1", dest="r1", type=Path, required=True)
    ap.add_argument("-r2", "--r2", dest="r2", type=Path, default=None)
    ap.add_argument("--outdir", type=Path, required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--module", default="FLU")
    args = ap.parse_args(argv)
    manifest = run(args.r1, args.outdir, args.name, r2=args.r2, module=args.module)
    return 0 if manifest.get("return_code", 1) == 0 else manifest.get("return_code", 1)


if __name__ == "__main__":
    sys.exit(main())
