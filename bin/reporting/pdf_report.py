"""
IRMA PDF report (reportlab + matplotlib).

Pure-Python PDF — no headless browser — so it renders reliably on any OOD host.
matplotlib figures are best-effort: if matplotlib is unavailable the report is
still produced, just without the charts.

Layout: title + subtype banner, a plain-language analysis summary, input-file
quality (read QC), per-segment assembly/coverage (with a depth figure), GenoFLU
genotype, HA cleavage, the submission strain name, and a methods/provenance page
with the standards referenced and a surveillance-interpretation disclaimer.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Tuple

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    Image,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

TEAL = colors.HexColor("#4C8C8A")
TERRA = colors.HexColor("#C88F7A")
INK = colors.HexColor("#1F2A2E")
MUTED = colors.HexColor("#6E7B82")
BORDER = colors.HexColor("#E3DED6")
DANGER = colors.HexColor("#C46A6A")
SUCCESS = colors.HexColor("#6BAA75")
WARN = colors.HexColor("#D8B26E")

_VERDICT_FILL = {"PASS": SUCCESS, "REVIEW": WARN, "FAIL": DANGER}


def _styles():
    ss = getSampleStyleSheet()
    ss.add(ParagraphStyle("H1", parent=ss["Title"], textColor=INK, fontSize=20, spaceAfter=2))
    ss.add(ParagraphStyle("Sub", parent=ss["Normal"], textColor=MUTED, fontSize=10, spaceAfter=10))
    ss.add(ParagraphStyle("H2", parent=ss["Heading2"], textColor=TEAL, fontSize=13,
                          spaceBefore=12, spaceAfter=4))
    ss.add(ParagraphStyle("Body", parent=ss["Normal"], textColor=INK, fontSize=9.5,
                          leading=13, alignment=TA_LEFT, spaceAfter=4))
    ss.add(ParagraphStyle("Small", parent=ss["Normal"], textColor=MUTED, fontSize=8, leading=10))
    ss.add(ParagraphStyle("Cell", parent=ss["Normal"], textColor=INK, fontSize=8.5, leading=11))
    return ss


def _kv_table(rows: List[Tuple[str, str]], ss, col0=2.4 * inch, col1=4.4 * inch) -> Table:
    data = [[Paragraph(f"<b>{k}</b>", ss["Cell"]), Paragraph(str(v), ss["Cell"])] for k, v in rows]
    t = Table(data, colWidths=[col0, col1])
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, -2), 0.4, BORDER),
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [colors.white, colors.HexColor("#FBFAF8")]),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
    ]))
    return t


def _banner(text: str, fill, ss) -> Table:
    t = Table([[Paragraph(f'<font color="white"><b>{text}</b></font>', ss["Body"])]],
              colWidths=[6.9 * inch])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), fill),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
    ]))
    return t


def _grid(data, ss, col_in, small=False, verdict_col=None):
    style = ss["Small"] if small else ss["Cell"]
    body = [[Paragraph(str(c), style) for c in row] for row in data]
    t = Table(body, colWidths=[c * inch for c in col_in], repeatRows=1)
    cmds = [
        ("BACKGROUND", (0, 0), (-1, 0), TEAL),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F6F5F2")]),
        ("GRID", (0, 0), (-1, -1), 0.3, BORDER),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 2.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
    ]
    # Colour the verdict cell per row when asked.
    if verdict_col is not None:
        for r in range(1, len(data)):
            v = str(data[r][verdict_col]).upper()
            fill = _VERDICT_FILL.get(v)
            if fill:
                cmds.append(("BACKGROUND", (verdict_col, r), (verdict_col, r), fill))
                cmds.append(("TEXTCOLOR", (verdict_col, r), (verdict_col, r), colors.white))
    t.setStyle(TableStyle(cmds))
    return t


# ---------------------------------------------------------------------------
# Figures (best-effort)
# ---------------------------------------------------------------------------
def _depth_bar(segments: List[Dict[str, Any]], outpath: Path) -> bool:
    if not segments:
        return False
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        labels = [s.get("gene", "?") for s in segments]
        vals = [float(s.get("avg_depth") or 0) for s in segments]
        cmap = {"PASS": "#6BAA75", "REVIEW": "#D8B26E", "FAIL": "#C46A6A"}
        bar_colors = [cmap.get(str(s.get("verdict")).upper(), "#4C8C8A") for s in segments]
        fig, ax = plt.subplots(figsize=(6.6, max(1.6, 0.42 * len(labels) + 0.6)))
        ax.barh(labels, vals, color=bar_colors)
        ax.axvline(10, color="#1F2A2E", lw=0.8, ls="--")
        ax.set_xlabel("average depth of coverage (X)  — dashed line = 10X")
        ax.set_title("Per-segment coverage depth", color="#1F2A2E", fontsize=11)
        for i, v in enumerate(vals):
            ax.text(v, i, f" {v:.0f}X", va="center", fontsize=8, color="#1F2A2E")
        ax.invert_yaxis()
        ax.spines[["top", "right"]].set_visible(False)
        fig.tight_layout()
        fig.savefig(outpath, dpi=150)
        plt.close(fig)
        return True
    except Exception:
        return False


def _read_cov_depths(path: Path):
    """Return (reference_name, [depth per position]) from an IRMA coverage table.
    Mirrors irma_pipeline._read_coverage_table so the report is self-contained."""
    import csv
    ref = ""
    depths: List[float] = []
    try:
        with path.open(newline="", encoding="utf-8", errors="replace") as fh:
            reader = csv.DictReader(fh, delimiter="\t")
            depth_key = ref_key = None
            for fld in (reader.fieldnames or []):
                low = (fld or "").strip().lower()
                if low in ("coverage depth", "coverage_depth", "depth"):
                    depth_key = fld
                elif low in ("reference_name", "reference name"):
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


def _cov_gene(ref: str) -> str:
    parts = (ref or "").split("_")
    return parts[1].upper() if len(parts) >= 2 and parts[1] else (parts[0].upper() if parts else "?")


def _coverage_plot(ref: str, depths: List[float], outpath: Path) -> bool:
    """Per-position coverage-depth plot for one segment (matplotlib)."""
    if not depths:
        return False
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        x = list(range(1, len(depths) + 1))
        fig, ax = plt.subplots(figsize=(6.8, 2.2))
        ax.fill_between(x, depths, color="#4C8C8A", alpha=0.35, linewidth=0)
        ax.plot(x, depths, color="#2F6F6C", lw=0.7)
        ax.axhline(10, color="#C46A6A", lw=0.8, ls="--")
        ax.set_xlabel("reference position (nt)  — dashed line = 10X")
        ax.set_ylabel("depth (X)")
        ax.margins(x=0)
        ax.spines[["top", "right"]].set_visible(False)
        fig.tight_layout()
        fig.savefig(outpath, dpi=150)
        plt.close(fig)
        return True
    except Exception:
        return False


def _coverage_section(outdir: Path, assets: Path, ss) -> List[Any]:
    """Build the appended per-segment IRMA coverage plots (one per segment),
    regenerated from IRMA's own coverage tables under <outdir>/irma/tables/."""
    tables_dir = outdir / "irma" / "tables"
    cov_tables = sorted(tables_dir.glob("*-coverage.txt")) if tables_dir.is_dir() else []
    if not cov_tables:
        return []
    try:
        import metadata as meta_mod
        seg_num = meta_mod.SEGMENT_NUMBER
    except Exception:
        seg_num = {}

    entries = []
    for tbl in cov_tables:
        ref, depths = _read_cov_depths(tbl)
        if not ref:
            ref = tbl.name.replace("-coverage.txt", "")
        gene = _cov_gene(ref)
        entries.append((seg_num.get(gene, 99), gene, ref, depths))
    entries.sort(key=lambda e: (e[0], e[2]))

    story: List[Any] = [PageBreak(),
                        Paragraph("IRMA per-segment coverage", ss["H2"]),
                        Paragraph(
                            "Per-position read depth for each assembled segment, from IRMA's coverage "
                            "tables. The dashed line marks the 10X minimum used in the QC verdict.",
                            ss["Body"])]
    n = 0
    for order, gene, ref, depths in entries:
        fig = assets / f"cov_{ref}.png"
        if not _coverage_plot(ref, depths, fig):
            continue
        seg_txt = f"segment {order} " if order != 99 else ""
        story.append(Paragraph(f"IRMA coverage — {seg_txt}{gene} ({ref})", ss["Body"]))
        story.append(Image(str(fig), width=6.6 * inch, height=_img_h(fig, 6.6)))
        story.append(Spacer(1, 6))
        n += 1
    return story if n else []


def _img_h(path: Path, width_in: float) -> float:
    try:
        from PIL import Image as PILImage
        with PILImage.open(path) as im:
            w, h = im.size
        return width_in * (h / w) * inch
    except Exception:
        return 2.4 * inch


def _i(v):
    try:
        return f"{int(float(v)):,}"
    except (TypeError, ValueError):
        return "—" if v in (None, "") else str(v)


def _f(v):
    try:
        return f"{float(v):.1f}"
    except (TypeError, ValueError):
        return "—" if v in (None, "") else str(v)


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------
def write_pdf(ctx: Dict[str, Any], path: Path, outdir: Path) -> None:
    ss = _styles()
    sample = ctx["sample"]
    fq = ctx.get("fastq_qc") or {}
    asm = ctx.get("asm") or {}
    cleavage = ctx.get("cleavage") or {}
    genoflu = ctx.get("genoflu") or {}
    man = ctx.get("manifest") or {}
    opts = man.get("options", {}) or {}
    vers = man.get("versions", {}) or {}
    rec = man.get("metadata_record", {}) or {}
    segments = asm.get("segments", []) or []
    subtype = asm.get("subtype", "—")

    assets = outdir / "_report_assets"
    assets.mkdir(exist_ok=True)

    story: List[Any] = []
    story.append(Paragraph("IRMA Influenza/Respiratory Virus Assembly Report", ss["H1"]))
    story.append(Paragraph(
        f"Sample <b>{sample}</b> &nbsp;·&nbsp; {ctx['date']} &nbsp;·&nbsp; "
        f"IRMA module {opts.get('module','?')} &nbsp;·&nbsp; IRMA {vers.get('irma','?')}",
        ss["Sub"]))

    verdict = (asm.get("overall_verdict") or "fail").upper()
    bfill = {"PASS": SUCCESS, "REVIEW": WARN, "FAIL": DANGER}.get(verdict, MUTED)
    geno = genoflu.get("genotype") or "(not assigned)"
    story.append(_banner(
        f"Subtype {subtype}  ·  {asm.get('segment_count',0)} segment(s)  ·  "
        f"GenoFLU genotype: {geno}  ·  assembly QC: {verdict}", bfill, ss))
    if cleavage.get("multibasic"):
        story.append(Spacer(1, 4))
        story.append(_banner(
            "⚠ HA multibasic cleavage motif detected — molecular indicator consistent with "
            "high-pathogenicity avian influenza (HPAI). Confirm by reference method.",
            DANGER, ss))
    story.append(Spacer(1, 8))

    # --- Analysis summary ---
    story.append(Paragraph("Analysis summary", ss["H2"]))
    strain = _strain(rec, subtype)
    summary = (
        f"Reads were assembled with CDC IRMA ({opts.get('module','FLU')} module), which iteratively "
        f"gathers reads and refines a per-segment consensus. This sample assembled "
        f"<b>{asm.get('segment_count',0)}</b> segment(s) with predicted subtype <b>{subtype}</b>. "
    )
    if geno and geno != "(not assigned)":
        summary += f"GenoFLU assigned genotype <b>{geno}</b> from the per-segment lineage pattern. "
    summary += (f"The assembled segments were re-headed into a submission FASTA under the strain "
                f"name <b>{strain}</b>. Overall assembly QC verdict: <b>{verdict}</b> "
                f"(PASS requires ≥10X mean depth, &lt;10% positions below 10X, ≤2% zero-coverage per segment).")
    story.append(Paragraph(summary, ss["Body"]))

    # --- Input file quality ---
    story.append(Paragraph("Input file quality", ss["H2"]))
    files = fq.get("files") or {}
    if files:
        story.append(Paragraph(
            "Per-FASTQ read statistics from <i>seqkit stats</i> (ISO 20397-2 read metrics). "
            "Q20/Q30 are the percentage of bases at or above those Phred quality scores; higher is better.",
            ss["Body"]))
        data = [["File", "Reads", "Bases (bp)", "Avg len", "GC%", "Q20%", "Q30%", "Avg Q"]]
        for tag in ("R1", "R2"):
            s = files.get(tag)
            if not s:
                continue
            data.append([tag, _i(s.get("num_seqs")), _i(s.get("sum_len")), _i(s.get("avg_len")),
                         _f(s.get("gc_pct")), _f(s.get("q20_pct")), _f(s.get("q30_pct")),
                         str(s.get("avg_qual", "—"))])
        story.append(_grid(data, ss, [0.7, 0.9, 1.2, 0.8, 0.7, 0.7, 0.7, 0.7]))
    else:
        story.append(Paragraph("No read-quality metrics available (seqkit unavailable).", ss["Body"]))

    # --- Per-segment assembly / coverage ---
    story.append(Paragraph("Assembly &amp; per-segment coverage", ss["H2"]))
    fig = assets / "depth.png"
    if _depth_bar(segments, fig):
        story.append(Image(str(fig), width=6.4 * inch, height=_img_h(fig, 6.4)))
    if segments:
        story.append(Paragraph(
            "Each segment's consensus length, mean depth, the fraction of positions below 10X, the "
            "fraction with zero coverage, and the QC verdict.", ss["Body"]))
        data = [["Segment", "Ref", "Length", "Mean depth", "% < 10X", "% zero", "QC"]]
        for s in segments:
            data.append([s.get("gene", ""), s.get("reference_name", ""), _i(s.get("length")),
                         f"{_f(s.get('avg_depth'))}X", _f(s.get("pct_lt10x")),
                         _f(s.get("pct_zero_cov")), str(s.get("verdict", ""))])
        story.append(_grid(data, ss, [0.8, 1.5, 0.9, 1.0, 0.8, 0.8, 0.8], verdict_col=6))
    else:
        story.append(_banner("No segments assembled — IRMA produced no consensus for this sample.",
                             DANGER, ss))

    # --- GenoFLU genotype detail ---
    story.append(Paragraph("GenoFLU genotype", ss["H2"]))
    gsegs = genoflu.get("segments") or []
    story.append(_kv_table([
        ("Genotype", geno),
        ("Segments matched", _i(genoflu.get("segments_matched"))),
        ("Complete", "yes" if genoflu.get("complete") else "no"),
    ], ss))
    if gsegs:
        data = [["Segment", "Lineage", "% identity", "Mismatches"]]
        for s in gsegs:
            data.append([s.get("segment", ""), s.get("lineage", "") or "—",
                         s.get("percent_identity", "") or "—", s.get("mismatches", "") or "—"])
        story.append(Spacer(1, 4))
        story.append(_grid(data, ss, [1.0, 1.2, 1.2, 1.2], small=True))

    # --- HA cleavage ---
    if cleavage:
        story.append(Paragraph("HA cleavage site", ss["H2"]))
        story.append(_kv_table([
            ("Cleavage motif (HA1 | HA2)", cleavage.get("motif") or "not determined"),
            ("Multibasic (HPAI indicator)",
             "yes" if cleavage.get("multibasic") else ("no" if cleavage.get("multibasic") is False else "—")),
            ("Note", cleavage.get("note") or "—"),
        ], ss))

    # --- Submission FASTA ---
    story.append(Paragraph("Submission sequence", ss["H2"]))
    story.append(_kv_table([
        ("Strain name", strain),
        ("Organism", rec.get("organism") or "Influenza A virus"),
        ("Host/species", rec.get("host") or "—"),
        ("State/location", rec.get("state") or "—"),
        ("Collection year", rec.get("collection_year") or "—"),
        ("Submission FASTA", Path(man.get("submission_fasta") or "—").name),
    ], ss))

    # --- Methods & provenance ---
    story.append(Paragraph("Methods &amp; provenance", ss["H2"]))
    iso = ", ".join(r.get("standard", "") for r in (man.get("iso_references") or []) if r.get("standard"))
    story.append(_kv_table([
        ("IRMA", f"{vers.get('irma','—')} (module {opts.get('module','—')})"),
        ("samtools / seqkit", f"{vers.get('samtools','—')} / {vers.get('seqkit','—')}"),
        ("Metadata source", Path(man.get("metadata_source") or "—").name),
        ("Standards referenced", iso or "—"),
    ], ss))
    story.append(Spacer(1, 6))
    story.append(Paragraph(
        "Disclaimer: subtype, genotype and cleavage-motif calls are genotypic predictions from a "
        "consensus assembly and curated reference databases. They support, but do not replace, "
        "confirmatory testing. HPAI determination must follow the responsible animal-health authority's "
        "validated procedures (WOAH Terrestrial Manual 3.3.4). Submit sequences under the strain name "
        "shown above only after metadata review.", ss["Small"]))

    # --- Appended IRMA per-segment coverage plots (one per segment) ---
    story.extend(_coverage_section(outdir, assets, ss))

    doc = SimpleDocTemplate(
        str(path), pagesize=letter,
        topMargin=0.6 * inch, bottomMargin=0.6 * inch,
        leftMargin=0.7 * inch, rightMargin=0.7 * inch,
        title=f"IRMA report — {sample}", author="irma_gui",
    )
    doc.build(story)


def _strain(rec: Dict[str, str], subtype) -> str:
    try:
        import metadata as meta_mod
        return meta_mod.strain_string(rec or {}, subtype)
    except Exception:
        return (rec or {}).get("sample") or "—"
