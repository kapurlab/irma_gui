#!/usr/bin/env python
"""
irma_pipeline.py — orchestrator for the IRMA GUI.

Pipeline (per sample):
  1. Input-file QC — `seqkit stats -a` on the reads -> fastq_qc.json
     (reads, length, GC, Q20/Q30, avg quality). These are the "quality stats of
     the input files" surfaced in the report (ISO 20397-2 read metrics).
  2. IRMA assembly — run_irma.py runs CDC IRMA (FLU / CoV) -> <outdir>/irma/.
  3. Assembly + coverage gathering — parse irma/tables/*-coverage.txt into
     per-segment depth / %<10X / %zero-coverage + subtype (H/N) -> assembly_stats.json,
     and concatenate the amended consensus -> assembly.fasta.
  4. Metadata + submission FASTA — re-write the assembled segment headers into
     <sample>-submission.fasta using per-sample metadata (web JSON or Excel,
     matched on the sample name). This is the deliverable a lab submits.
  5. HA cleavage site (influenza, best-effort) — translate HA, report the
     HA1/HA2 cleavage motif (HPAI multibasic indicator).
  6. GenoFLU — run_genoflu.py genotypes the assembled influenza-A genome.
  7. Report — reporting.build() writes <sample>_<date>_stats.xlsx (single
     labeled column, vSNP3-style) and report.pdf.

Output dir is <project>/irma/<sample>/ (passed via --outdir). Every artifact
lands there with a stable name so the backend's result endpoints find it.

Usage:
  irma_pipeline.py --sample S --outdir DIR -r1 R1 [-r2 R2]
      [--module FLU] [--no-genoflu] [--genoflu-db DIR] [--genoflu-pident 98.0]
      [--metadata-xlsx FILE [--metadata-key-col COL]]
"""

# --- provenance: log every external command this pipeline runs (best-effort) ---
# Captures commands launched directly by this orchestrator. Child executables
# remain responsible for their own nested-command provenance.
def _install_provenance_capture():
    import os as _o, subprocess as _s, shlex as _sh, sys as _sys
    from pathlib import Path as _P
    from datetime import datetime as _dt
    _tool = _P(__file__).resolve().parents[1].name
    _argv = _sys.argv[1:]
    _dest = next((_argv[i + 1] for i, x in enumerate(_argv[:-1]) if x == "--outdir"), None)
    _dest = next((x.split("=", 1)[1] for x in _argv if x.startswith("--outdir=")), _dest)
    _out = _P(_dest).expanduser().resolve() / ".provenance" if _dest else _P.cwd() / ".provenance"
    _f = _out / (_tool + "_commands.txt")
    def _log(_cmd, _cwd=None):
        try:
            _out.mkdir(parents=True, exist_ok=True)
            _ln = _cmd if isinstance(_cmd, str) else _sh.join(str(c) for c in _cmd)
            _ts = _dt.now().astimezone().strftime("%H:%M:%S")
            with open(_f, "a", encoding="utf-8") as _h:
                _where = _P(_cwd).resolve() if _cwd else _P.cwd().resolve()
                _h.write(_ts + "  cwd=" + _sh.quote(str(_where)) + "  " + _ln + "\n")
        except Exception:
            pass
    try:
        _out.mkdir(parents=True, exist_ok=True)
        with open(_f, "a", encoding="utf-8") as _h:
            _h.write("\n# === %s run %s — external commands that produced results in this folder ===\n"
                     % (_tool, _dt.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")))
    except Exception:
        pass
    _orig_popen = _s.Popen
    class _Popen(_orig_popen):
        def __init__(self, args, *a, **k):
            _log(args, k.get("cwd"))
            super().__init__(args, *a, **k)
    _s.Popen = _Popen
    _osys = _o.system
    def _sysw(_cmd):
        _log(_cmd)
        return _osys(_cmd)
    _o.system = _sysw
try:
    _install_provenance_capture()
except Exception:
    pass
# --- end provenance ------------------------------------------------------------


import argparse
import csv
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import metadata as meta_mod      # local import (PYTHONPATH includes bin/)
import run_irma

# Influenza-A reference segment lengths (coding genome) for coverage-of-expected.
FLU_SEG_LEN = {"PB2": 2280, "PB1": 2274, "PA": 2151, "HA": 1701,
               "NP": 1497, "NA": 1410, "MP": 982, "NS": 838}
SEGMENT_ORDER = ["PB2", "PB1", "PA", "HA", "NP", "NA", "MP", "NS"]
SEGMENT_NUMBER = meta_mod.SEGMENT_NUMBER


def log(msg: str) -> None:
    print(msg, flush=True)


def step(title: str) -> None:
    log("")
    log(f"### {title}")


def _have(tool: str) -> bool:
    import shutil
    return shutil.which(tool) is not None


def _write(path: Path, obj: Any) -> None:
    Path(path).write_text(json.dumps(obj, indent=2) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Step 1 — input FASTQ QC (seqkit stats -a)
# ---------------------------------------------------------------------------
def fastq_qc(r1: Optional[Path], r2: Optional[Path], outdir: Path) -> Dict[str, Any]:
    qc: Dict[str, Any] = {"files": {}, "notes": []}
    if not _have("seqkit"):
        qc["notes"].append("seqkit not on PATH — input read QC unavailable.")
        _write(outdir / "fastq_qc.json", qc)
        return qc
    for tag, path in (("R1", r1), ("R2", r2)):
        if not path:
            continue
        try:
            proc = subprocess.run(["seqkit", "stats", "-T", "-a", str(path)],
                                  capture_output=True, text=True, timeout=900)
            lines = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]
            if len(lines) >= 2:
                row = dict(zip(lines[0].split("\t"), lines[1].split("\t")))

                def num(k):
                    try:
                        return float(str(row.get(k, "")).replace(",", ""))
                    except (ValueError, AttributeError):
                        return None

                qc["files"][tag] = {
                    "file": Path(path).name,
                    "num_seqs": num("num_seqs"),
                    "sum_len": num("sum_len"),
                    "min_len": num("min_len"),
                    "avg_len": num("avg_len"),
                    "max_len": num("max_len"),
                    "n50": num("N50"),
                    "gc_pct": num("GC(%)"),
                    "q20_pct": num("Q20(%)"),
                    "q30_pct": num("Q30(%)"),
                    "avg_qual": (row.get("AvgQual", "") or "").strip() or None,
                }
        except (subprocess.SubprocessError, OSError) as exc:
            qc["notes"].append(f"seqkit stats failed for {tag}: {exc}")
    _write(outdir / "fastq_qc.json", qc)
    return qc


# ---------------------------------------------------------------------------
# Step 3 — assembly + coverage gathering
# ---------------------------------------------------------------------------
def _gene_of(reference_name: str) -> str:
    """IRMA reference names look like A_HA_H5 / A_PB2 / B_HA. Return the gene
    token (HA, PB2, ...); for SARS-CoV-2 return the whole name."""
    parts = reference_name.split("_")
    if len(parts) >= 2 and parts[1]:
        return parts[1].upper()
    return parts[0].upper()


def _read_coverage_table(path: Path) -> Tuple[str, List[float]]:
    """Return (reference_name, [depth per position]) from an IRMA coverage table."""
    ref = ""
    depths: List[float] = []
    try:
        with path.open(newline="", encoding="utf-8", errors="replace") as fh:
            reader = csv.DictReader(fh, delimiter="\t")
            depth_key = None
            for fld in (reader.fieldnames or []):
                if fld and fld.strip().lower() in ("coverage depth", "coverage_depth", "depth"):
                    depth_key = fld
            ref_key = None
            for fld in (reader.fieldnames or []):
                if fld and fld.strip().lower() in ("reference_name", "reference name"):
                    ref_key = fld
            for row in reader:
                if ref_key and not ref:
                    ref = (row.get(ref_key) or "").strip()
                if depth_key is not None:
                    try:
                        depths.append(float(row.get(depth_key) or 0))
                    except (ValueError, TypeError):
                        depths.append(0.0)
    except OSError:
        pass
    return ref, depths


def _segment_verdict(avg_depth: float, pct_lt10: float, pct_zero: float) -> str:
    if avg_depth >= 10 and pct_lt10 < 10 and pct_zero <= 2:
        return "PASS"
    if avg_depth >= 10 and (pct_lt10 <= 50 or pct_zero <= 10):
        return "REVIEW"
    return "FAIL"


def gather_assembly(irma_dir: Path, outdir: Path, module: str) -> Dict[str, Any]:
    """Concatenate IRMA consensus -> assembly.fasta and parse coverage tables
    into per-segment metrics + subtype. Returns assembly_stats dict."""
    stats: Dict[str, Any] = {
        "module": module, "subtype": "H?N?", "h_type": "H?", "n_type": "N?",
        "segment_count": 0, "segments": [], "assembly_fasta": None,
        "overall_verdict": "fail", "seq_by_gene": {}, "notes": [],
    }

    # Concatenate per-segment consensus FASTAs (raw IRMA headers).
    seg_fastas = sorted(irma_dir.glob("*.fasta"))
    seq_by_gene: Dict[str, str] = {}
    ref_by_gene: Dict[str, str] = {}
    assembly_path = outdir / "assembly.fasta"
    if seg_fastas:
        with assembly_path.open("w", encoding="utf-8") as out:
            for fa in seg_fastas:
                text = fa.read_text(encoding="utf-8", errors="replace")
                out.write(text if text.endswith("\n") else text + "\n")
                # capture sequence per gene
                header = ""
                seq_parts: List[str] = []
                for line in text.splitlines():
                    if line.startswith(">"):
                        if header and seq_parts:
                            g = _gene_of(header)
                            seq_by_gene[g] = "".join(seq_parts)
                            ref_by_gene[g] = header
                        header = line[1:].strip().split()[0]
                        seq_parts = []
                    else:
                        seq_parts.append(line.strip())
                if header and seq_parts:
                    g = _gene_of(header)
                    seq_by_gene[g] = "".join(seq_parts)
                    ref_by_gene[g] = header
        stats["assembly_fasta"] = str(assembly_path)
    else:
        stats["notes"].append("No IRMA consensus FASTA — assembly failed.")
        _write_assembly_stats(outdir, stats)
        return stats

    # Per-segment coverage metrics from irma/tables/*-coverage.txt.
    tables_dir = irma_dir / "tables"
    cov_tables = sorted(tables_dir.glob("*-coverage.txt")) if tables_dir.is_dir() else []
    segments: List[Dict[str, Any]] = []
    h_type, n_type = "H?", "N?"
    for tbl in cov_tables:
        ref, depths = _read_coverage_table(tbl)
        if not ref:
            ref = tbl.name.replace("-coverage.txt", "")
        gene = _gene_of(ref)
        contig_len = len(depths)
        if contig_len == 0:
            continue
        # Subtype from reference name suffix (A_HA_H5 -> H5; A_NA_N1 -> N1).
        if gene == "HA" and "_" in ref:
            h_type = ref.split("_")[-1] or h_type
        if gene == "NA" and "_" in ref:
            n_type = ref.split("_")[-1] or n_type
        expected = FLU_SEG_LEN.get(gene)
        if module.upper().startswith("FLU") and expected:
            zero = max(expected - contig_len, 0)
            avg_depth = sum(depths) / expected
            lt10 = sum(1 for d in depths if d < 10) + zero
            pct_lt10 = lt10 / expected * 100
            pct_zero = zero / expected * 100
        else:
            expected = contig_len
            avg_depth = sum(depths) / contig_len
            lt10 = sum(1 for d in depths if d < 10)
            pct_lt10 = lt10 / contig_len * 100
            pct_zero = sum(1 for d in depths if d == 0) / contig_len * 100
        verdict = _segment_verdict(avg_depth, pct_lt10, pct_zero)
        segments.append({
            "gene": gene,
            "reference_name": ref,
            "segment_number": SEGMENT_NUMBER.get(gene),
            "length": len(seq_by_gene.get(gene, "")) or contig_len,
            "covered_positions": contig_len,
            "expected_length": expected,
            "avg_depth": round(avg_depth, 1),
            "pct_lt10x": round(pct_lt10, 1),
            "pct_zero_cov": round(pct_zero, 1),
            "verdict": verdict,
        })

    def _order(s):
        g = s.get("gene", "")
        return SEGMENT_ORDER.index(g) if g in SEGMENT_ORDER else len(SEGMENT_ORDER)

    segments.sort(key=_order)
    subtype = f"{h_type}{n_type}" if module.upper().startswith("FLU") else module
    verdicts = [s["verdict"] for s in segments]
    if module.upper().startswith("FLU"):
        overall = "pass" if (len(segments) == 8 and all(v == "PASS" for v in verdicts)) else (
            "review" if any(v in ("PASS", "REVIEW") for v in verdicts) else "fail")
    else:
        overall = "pass" if verdicts and all(v == "PASS" for v in verdicts) else (
            "review" if any(v in ("PASS", "REVIEW") for v in verdicts) else "fail")

    stats.update({
        "subtype": subtype, "h_type": h_type, "n_type": n_type,
        "segment_count": len(segments), "segments": segments,
        "overall_verdict": overall, "seq_by_gene": seq_by_gene,
        "reference_by_gene": ref_by_gene,
    })
    _write_assembly_stats(outdir, stats)
    return stats


def _write_assembly_stats(outdir: Path, stats: Dict[str, Any]) -> None:
    """Persist assembly_stats.json without the bulky per-gene sequences (those
    stay in assembly.fasta); the in-memory dict keeps them for downstream use."""
    on_disk = {k: v for k, v in stats.items() if k != "seq_by_gene"}
    _write(outdir / "assembly_stats.json", on_disk)


# ---------------------------------------------------------------------------
# Step 4 — submission FASTA (header re-write from metadata)
# ---------------------------------------------------------------------------
def make_submission_fasta(sample: str, assembly_stats: Dict[str, Any], record: Dict[str, str],
                          outdir: Path, header_style: str = "ncbi") -> Optional[Path]:
    """Write <sample>-submission.fasta: the assembled segment sequences with
    deflines rebuilt from the sample's metadata (strain name + organism)."""
    seq_by_gene = assembly_stats.get("seq_by_gene") or {}
    if not seq_by_gene:
        log("No assembled sequences — skipping submission FASTA.")
        return None
    subtype = assembly_stats.get("subtype")
    out = outdir / f"{sample}-submission.fasta"
    genes = [g for g in SEGMENT_ORDER if g in seq_by_gene] or sorted(seq_by_gene)
    with out.open("w", encoding="utf-8") as fh:
        for gene in genes:
            seq = seq_by_gene[gene].replace("-", "").replace(".", "")
            if not seq:
                continue
            defline = meta_mod.submission_defline(record, gene, subtype, style=header_style)
            fh.write(f">{defline}\n")
            for i in range(0, len(seq), 70):
                fh.write(seq[i:i + 70] + "\n")
    strain = meta_mod.strain_string(record, subtype)
    log(f"Wrote submission FASTA with strain header: {strain}  ({len(genes)} segment(s))")
    return out


# ---------------------------------------------------------------------------
# Step 5 — HA cleavage site (influenza, best-effort)
# ---------------------------------------------------------------------------
def ha_cleavage_site(assembly_stats: Dict[str, Any], outdir: Path) -> Dict[str, Any]:
    """Translate the HA segment and report the HA1/HA2 cleavage motif. A
    multibasic motif (e.g. ...RRRKR/GLF) is the molecular HPAI indicator."""
    result: Dict[str, Any] = {"motif": None, "multibasic": None, "note": ""}
    ha = (assembly_stats.get("seq_by_gene") or {}).get("HA")
    if not ha:
        result["note"] = "No HA segment assembled."
        _write(outdir / "ha_cleavage.json", result)
        return result
    try:
        from Bio.Seq import Seq
        clean = re.sub(r"[^ACGTUacgtu]", "", ha)
        best = ""
        for frame in range(3):
            sub = clean[frame:]
            sub = sub[: len(sub) - (len(sub) % 3)]
            for pro in str(Seq(sub).translate()).split("*"):
                if len(pro) > len(best):
                    best = pro
        # The HA0 cleavage site precedes the conserved fusion peptide "GLF"
        # at the HA2 N-terminus. Report the basic residues immediately before it.
        idx = best.find("GLF")
        if idx > 6:
            motif = best[idx - 8:idx] + "|" + best[idx:idx + 3]
            basic = best[idx - 8:idx]
            n_basic = sum(1 for aa in basic if aa in ("R", "K"))
            result["motif"] = motif
            result["multibasic"] = n_basic >= 4
            result["note"] = ("Multibasic cleavage motif (HPAI molecular indicator)."
                              if result["multibasic"] else
                              "Monobasic / low-pathogenicity-type cleavage motif.")
        else:
            result["note"] = "Cleavage motif (GLF fusion peptide) not located in translation."
    except Exception as exc:  # noqa: BLE001
        result["note"] = f"HA cleavage analysis unavailable: {exc}"
    _write(outdir / "ha_cleavage.json", result)
    if result.get("motif"):
        log(f"HA cleavage motif: {result['motif']}  ({result['note']})")
    return result


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="IRMA pipeline orchestrator.")
    ap.add_argument("--sample", required=True)
    ap.add_argument("--outdir", type=Path, required=True)
    ap.add_argument("-r1", "--r1", dest="r1", type=Path, required=True)
    ap.add_argument("-r2", "--r2", dest="r2", type=Path, default=None)
    ap.add_argument("--module", default="FLU")
    ap.add_argument("--no-genoflu", action="store_true", default=False)
    ap.add_argument("--genoflu-db", default=None)
    ap.add_argument("--genoflu-pident", type=float, default=98.0)
    ap.add_argument("--metadata-xlsx", default=None,
                    help="External metadata Excel (matched on the sample name).")
    ap.add_argument("--metadata-key-col", default=None,
                    help="Column in --metadata-xlsx holding the sample/accession.")
    ap.add_argument("--header-style", default="ncbi", choices=["ncbi", "strain"])
    args = ap.parse_args(argv)

    outdir: Path = args.outdir
    outdir.mkdir(parents=True, exist_ok=True)
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    module = (args.module or "FLU").strip()

    log("=" * 70)
    log(f"IRMA pipeline — sample: {args.sample}")
    log(f"  module:  {module}")
    log(f"  R1:      {args.r1}")
    log(f"  R2:      {args.r2 or '(single-end)'}")
    log(f"  outdir:  {outdir}")
    log("=" * 70)

    if not args.r1.exists():
        log(f"ERROR: R1 not found: {args.r1}")
        return 2

    # ---- Step 1: input read QC ----
    step("Step 1: Input file QC (seqkit stats on reads)")
    fq = fastq_qc(args.r1, args.r2, outdir)
    for tag, s in (fq.get("files") or {}).items():
        log(f"  {tag}: {s.get('num_seqs')} reads, Q30 {s.get('q30_pct')}%, "
            f"avgQ {s.get('avg_qual')}, GC {s.get('gc_pct')}%")

    # ---- Step 2: IRMA assembly ----
    step(f"Step 2: IRMA assembly (module {module})")
    # The submission-FASTA metadata lives in the project's irma/ dir (parent of
    # the per-sample run dir). Resolve the sample's record now so the manifest
    # records exactly which metadata produced the headers.
    irma_project_dir = outdir.parent
    record = meta_mod.get_metadata(
        args.sample, irma_dir=irma_project_dir,
        xlsx=Path(args.metadata_xlsx) if args.metadata_xlsx else None,
        key_col=args.metadata_key_col,
    )

    irma_manifest = run_irma.run(
        r1=args.r1, outdir=outdir, name=args.sample, r2=args.r2, module=module,
        extra_provenance={
            "pipeline": "irma_pipeline",
            "pipeline_started_at": started,
            "input_qc": fq,
            "metadata_record": record,
            "metadata_source": (str(args.metadata_xlsx) if args.metadata_xlsx
                                else str(irma_project_dir / meta_mod.METADATA_JSON)),
        },
    )
    irma_dir = outdir / "irma"
    irma_rc = irma_manifest.get("return_code", 1)

    # ---- Step 3: assembly + coverage ----
    step("Step 3: Assembly + coverage gathering")
    asm = gather_assembly(irma_dir, outdir, module)
    log(f"  {asm['segment_count']} segment(s); subtype {asm['subtype']}; "
        f"QC verdict: {asm['overall_verdict'].upper()}")
    for s in asm["segments"]:
        log(f"    {s['gene']:<4} depth {s['avg_depth']}X  <10X {s['pct_lt10x']}%  "
            f"zero {s['pct_zero_cov']}%  -> {s['verdict']}")

    # ---- Step 4: submission FASTA (header rewrite from metadata) ----
    step("Step 4: Submission FASTA (header rewrite from metadata)")
    submission = make_submission_fasta(args.sample, asm, record, outdir,
                                       header_style=args.header_style)

    # ---- Step 5: HA cleavage (influenza) ----
    cleavage: Dict[str, Any] = {}
    if module.upper().startswith("FLU"):
        step("Step 5: HA cleavage site")
        cleavage = ha_cleavage_site(asm, outdir)

    # ---- Step 6: GenoFLU ----
    genoflu_manifest: Dict[str, Any] = {}
    if module.upper().startswith("FLU") and not args.no_genoflu and asm.get("assembly_fasta"):
        step("Step 6: GenoFLU genotyping (assembled genome)")
        try:
            import run_genoflu
            genoflu_manifest = run_genoflu.run(
                fasta=Path(asm["assembly_fasta"]),
                outdir=outdir, name=args.sample,
                pident=args.genoflu_pident, genoflu_db=args.genoflu_db,
            )
            log(f"  GenoFLU genotype: {genoflu_manifest.get('genotype') or '(not assigned)'}")
        except Exception as exc:  # noqa: BLE001 — optional step never kills the run
            log(f"  WARNING: GenoFLU step failed: {exc}")
    elif args.no_genoflu:
        log("GenoFLU disabled (--no-genoflu).")

    # ---- Augment the run manifest with the full pipeline provenance ----
    manifest_path = outdir / "run_manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        manifest = irma_manifest
    manifest["pipeline_finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    manifest["assembly_stats"] = {k: v for k, v in asm.items() if k != "seq_by_gene"}
    manifest["ha_cleavage"] = cleavage
    manifest["submission_fasta"] = str(submission) if submission else None
    manifest["genoflu"] = {
        "genotype": genoflu_manifest.get("genotype"),
        "segments_matched": genoflu_manifest.get("segments_matched"),
        "return_code": genoflu_manifest.get("return_code"),
    } if genoflu_manifest else {"genotype": None, "note": "not run"}
    _write(manifest_path, manifest)

    # ---- Step 7: report ----
    step("Step 7: Building report (stats.xlsx + report.pdf)")
    try:
        import reporting  # bin/ on PYTHONPATH
        reporting.build(outdir, args.sample, log=log)
    except Exception as exc:  # noqa: BLE001 — never fail the run over the report
        log(f"  WARNING: report generation failed: {exc}")

    step("Pipeline completed")
    log(f"IRMA return code: {irma_rc}")
    log(f"Subtype: {asm['subtype']}  |  GenoFLU: {manifest['genoflu'].get('genotype') or '(n/a)'}")
    log(f"Outputs in: {outdir}")
    return 0 if irma_rc == 0 and asm["segment_count"] > 0 else (irma_rc or 1)


if __name__ == "__main__":
    sys.exit(main())
