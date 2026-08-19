"""
IRMA GUI — report builder.

Produces two deliverables from a completed per-sample run directory:

  <sample>_<date>_stats.xlsx
      A single labeled column of statistics (column A = label, column B =
      value), modelled on the vSNP3 stats workbook so the two tools read the
      same way. Input-file QC, per-segment coverage, subtype, GenoFLU genotype
      and provenance in one flat, labeled list.

  report.html
      A self-contained interactive report: every PDF section plus one zoomable
      Plotly "Coverage & Variants" chart per assembled segment — per-position
      depth with IRMA's minority-variant SNPs marked, zero-coverage stretches
      shaded, and the 10X QC floor drawn in.

  report.pdf
      The same document rendered to PDF by WeasyPrint (interactive charts
      swap to their static twins under print media). When WeasyPrint is not
      installed the PDF falls back to the original reportlab layout, so a
      PDF is produced either way.

All are best-effort: a missing artifact or optional dependency (plotly /
weasyprint / reportlab / matplotlib) degrades gracefully and is reported in
the log, never failing the pipeline.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def _load_json(path: Path) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _fmt_int(v: Any) -> str:
    try:
        return f"{int(float(v)):,}"
    except (TypeError, ValueError):
        return "—" if v in (None, "") else str(v)


def _fmt_pct(v: Any, dp: int = 1) -> str:
    try:
        return f"{float(v):.{dp}f}%"
    except (TypeError, ValueError):
        return "—" if v in (None, "") else str(v)


def _fastq_label(stats: Dict[str, Any], tag: str, items: List[Tuple[str, str]]) -> None:
    """Append vSNP3-style R1/R2 read-quality rows for one FASTQ file."""
    if not stats:
        return
    items.append((f"FASTQ_{tag}", stats.get("file", "—")))
    items.append((f"{tag} Read Count", _fmt_int(stats.get("num_seqs"))))
    items.append((f"{tag} Length Sum (bp)", _fmt_int(stats.get("sum_len"))))
    items.append((f"{tag} Min Length", _fmt_int(stats.get("min_len"))))
    items.append((f"{tag} Avg Length", _fmt_int(stats.get("avg_len"))))
    items.append((f"{tag} Max Length", _fmt_int(stats.get("max_len"))))
    items.append((f"{tag} GC (%)", _fmt_pct(stats.get("gc_pct"))))
    items.append((f"{tag} Q20 (%)", _fmt_pct(stats.get("q20_pct"))))
    items.append((f"{tag} Q30 (%)", _fmt_pct(stats.get("q30_pct"))))
    items.append((f"{tag} Read Quality Ave", stats.get("avg_qual", "—")))


# ---------------------------------------------------------------------------
# Build the ordered, labeled stats list (one metric per row)
# ---------------------------------------------------------------------------
def build_stats_items(
    sample: str,
    date_stamp: str,
    fastq_qc: Dict[str, Any],
    asm: Dict[str, Any],
    cleavage: Dict[str, Any],
    genoflu: Dict[str, Any],
    manifest: Dict[str, Any],
) -> List[Tuple[str, str]]:
    items: List[Tuple[str, str]] = []
    opts = manifest.get("options", {}) or {}
    vers = manifest.get("versions", {}) or {}
    rec = manifest.get("metadata_record", {}) or {}

    # — Sample —
    items.append(("sample", sample))
    items.append(("date", date_stamp))
    items.append(("Pipeline", "IRMA"))
    items.append(("IRMA module", opts.get("module", "—")))
    items.append(("Input mode", opts.get("input_mode", "—")))

    # — Metadata / strain —
    items.append(("Metadata host/species", rec.get("host") or "—"))
    items.append(("Metadata state/location", rec.get("state") or "—"))
    items.append(("Metadata collection year", rec.get("collection_year") or "—"))
    items.append(("Submission strain name", _strain(rec, asm.get("subtype"))))

    # — Input file quality (vSNP3-style) —
    files = (fastq_qc or {}).get("files", {})
    if files:
        _fastq_label(files.get("R1", {}), "R1", items)
        if files.get("R2"):
            _fastq_label(files.get("R2", {}), "R2", items)

    # — Assembly / subtype —
    items.append(("Subtype", asm.get("subtype", "—")))
    items.append(("Segments assembled", _fmt_int(asm.get("segment_count"))))
    items.append(("Assembly QC verdict", (asm.get("overall_verdict") or "—").upper()))

    # — Per-segment coverage (one labeled row per segment, vSNP3-style) —
    seg_len = []
    seg_cov = []
    for s in asm.get("segments", []):
        g = s.get("gene")
        seg_len.append(f"{g}:{_fmt_int(s.get('length'))}")
        seg_cov.append(f"{g}:{s.get('avg_depth')}X_{s.get('pct_lt10x')}%<10X_"
                       f"{s.get('pct_zero_cov')}%zero_{s.get('verdict')}")
    items.append(("Sequence Length Summary", " -- ".join(seg_len) if seg_len else "—"))
    items.append(("Per-segment Depth and %<10X and %zero and QC",
                  " -- ".join(seg_cov) if seg_cov else "—"))

    # — HA cleavage —
    if cleavage:
        items.append(("HA cleavage motif", cleavage.get("motif") or "—"))
        items.append(("HA multibasic (HPAI indicator)",
                      "yes" if cleavage.get("multibasic") else
                      ("no" if cleavage.get("multibasic") is False else "—")))

    # — GenoFLU —
    items.append(("GenoFLU genotype", genoflu.get("genotype") or "(not assigned)"))
    items.append(("GenoFLU segments matched", _fmt_int(genoflu.get("segments_matched"))))

    # — Methods / provenance —
    items.append(("IRMA version", vers.get("irma") or "—"))
    items.append(("samtools version", vers.get("samtools") or "—"))
    items.append(("seqkit version", vers.get("seqkit") or "—"))
    iso = [r.get("standard") for r in (manifest.get("iso_references") or []) if r.get("standard")]
    items.append(("Standards referenced", ", ".join(iso) if iso else "—"))
    return items


def _strain(rec: Dict[str, str], subtype: Optional[str]) -> str:
    try:
        import metadata as meta_mod
        return meta_mod.strain_string(rec or {}, subtype)
    except Exception:
        return rec.get("sample") or "—"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def build(outdir: Path, sample: str, log=print,
          write_xlsx: bool = True) -> Dict[str, Optional[str]]:
    outdir = Path(outdir)
    result: Dict[str, Optional[str]] = {"stats_xlsx": None, "report_html": None, "report_pdf": None}

    fastq_qc = _load_json(outdir / "fastq_qc.json")
    asm = _load_json(outdir / "assembly_stats.json")
    cleavage = _load_json(outdir / "ha_cleavage.json")
    genoflu = _load_json(outdir / "genoflu_result.json")
    manifest = _load_json(outdir / "run_manifest.json")

    date_stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    items = build_stats_items(sample, date_stamp, fastq_qc, asm, cleavage, genoflu, manifest)

    # The workbook's name carries a timestamp, so re-rendering a finished run's
    # REPORT would otherwise drop a duplicate workbook beside the original every
    # time. write_xlsx=False is for exactly that case.
    if write_xlsx:
        try:
            from .stats_excel import write_stats_xlsx
            xlsx_path = outdir / f"{sample}_{date_stamp}_stats.xlsx"
            write_stats_xlsx(items, xlsx_path, sample)
            result["stats_xlsx"] = str(xlsx_path)
            log(f"  wrote {xlsx_path.name}")
        except Exception as exc:  # noqa: BLE001
            log(f"  WARNING: stats workbook not written: {exc}")
    else:
        existing = sorted(outdir.glob(f"{sample}_*_stats.xlsx"))
        if existing:
            result["stats_xlsx"] = str(existing[-1])

    ctx = {
        "sample": sample, "date": date_stamp,
        "fastq_qc": fastq_qc, "asm": asm, "cleavage": cleavage,
        "genoflu": genoflu, "manifest": manifest, "stats_items": items,
    }

    # HTML first — it is the primary document now, with the interactive
    # per-segment Coverage & Variants charts.
    html_path = outdir / "report.html"
    try:
        from .html_report import write_html
        write_html(ctx, html_path, outdir)
        result["report_html"] = str(html_path)
        log(f"  wrote {html_path.name}")
    except Exception as exc:  # noqa: BLE001
        log(f"  WARNING: HTML report not written ({exc}).")

    # PDF: render the SAME HTML via WeasyPrint so both documents match; keep
    # the original reportlab layout as the fallback when WeasyPrint (or the
    # HTML itself) is unavailable, so a PDF is produced either way.
    pdf_path = outdir / "report.pdf"
    pdf_via_weasyprint = False
    if result["report_html"]:
        try:
            from .html_report import html_to_pdf
            pdf_via_weasyprint = html_to_pdf(html_path, pdf_path)
        except Exception:  # noqa: BLE001
            pdf_via_weasyprint = False
    if pdf_via_weasyprint:
        result["report_pdf"] = str(pdf_path)
        log(f"  wrote {pdf_path.name} (WeasyPrint, from report.html)")
    else:
        try:
            from .pdf_report import write_pdf
            write_pdf(ctx, pdf_path, outdir)
            result["report_pdf"] = str(pdf_path)
            log(f"  wrote {pdf_path.name} (reportlab fallback"
                " — install weasyprint for the HTML-matched PDF)")
        except Exception as exc:  # noqa: BLE001
            log(f"  WARNING: PDF report not written ({exc}). Is reportlab installed?")

    return result


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Build IRMA stats.xlsx + report.pdf for a run dir.")
    ap.add_argument("--outdir", type=Path, required=True)
    ap.add_argument("--sample", required=True)
    args = ap.parse_args()
    print(json.dumps(build(args.outdir, args.sample), indent=2))
