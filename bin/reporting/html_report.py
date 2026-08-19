"""
IRMA GUI — HTML report (+ WeasyPrint PDF of the same document).

Builds a single self-contained `report.html` carrying every section of the PDF
report — verdict banner, analysis summary, read QC, per-segment assembly table,
GenoFLU, HA cleavage, submission strain, methods/provenance — plus one
interactive Plotly "Coverage & Variants" chart per assembled segment: zoomable
per-position depth with IRMA's minority-variant SNPs marked on the curve,
zero-coverage stretches shaded, and the 10X QC floor drawn as a dashed line.

Self-contained by construction: plotly.js is inlined once, and every static
figure is embedded as a base64 data URI, so the file can be downloaded and
opened anywhere with no network. Each interactive chart ships a hidden PNG
twin that only print media shows — WeasyPrint renders with print media, so
`html_to_pdf()` turns this same HTML into `report.pdf` with static figures
where the interactive charts sit in a browser.

Plotly is best-effort like every other optional dependency here: without it
the charts degrade to the same static PNGs the PDF uses, and the report is
still produced.
"""

from __future__ import annotations

import base64
import html
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .pdf_report import (
    _cov_gene,
    _coverage_plot,
    _depth_bar,
    _read_cov_depths,
    _read_variants,
    _strain,
)

TEAL = "#4C8C8A"
TEAL_DARK = "#2F6F6C"
INK = "#1F2A2E"
MUTED = "#6E7B82"
BORDER = "#E3DED6"
DANGER = "#C46A6A"
SUCCESS = "#6BAA75"
WARN = "#D8B26E"

_VERDICT_COLOR = {"PASS": SUCCESS, "REVIEW": WARN, "FAIL": DANGER}

# The QC floor the whole tool grades against (assembly_stats' pct_lt10x and the
# PASS wording in the summary). The chart's threshold line must be the same
# number the verdicts use, or the report argues with itself.
QC_DEPTH_FLOOR = 10


def _e(v: Any) -> str:
    return html.escape("—" if v in (None, "") else str(v))


def _fmt_int(v: Any) -> str:
    try:
        return f"{int(float(v)):,}"
    except (TypeError, ValueError):
        return _e(v)


def _fmt_f(v: Any) -> str:
    try:
        return f"{float(v):.1f}"
    except (TypeError, ValueError):
        return _e(v)


def _kv_rows(rows: List[Tuple[str, Any]]) -> str:
    cells = "".join(f"<tr><th>{_e(k)}</th><td>{_e(v)}</td></tr>" for k, v in rows)
    return f'<table class="kv">{cells}</table>'


def _grid_html(head: List[str], body_rows: List[List[str]], verdict_col: Optional[int] = None) -> str:
    thead = "".join(f"<th>{_e(h)}</th>" for h in head)
    rows_html = []
    for row in body_rows:
        tds = []
        for i, cell in enumerate(row):
            if verdict_col is not None and i == verdict_col:
                color = _VERDICT_COLOR.get(str(cell).upper())
                if color:
                    tds.append(f'<td style="background:{color};color:#fff;font-weight:700">{_e(cell)}</td>')
                    continue
            tds.append(f"<td>{_e(cell)}</td>")
        rows_html.append(f"<tr>{''.join(tds)}</tr>")
    return (f'<div class="scroll"><table class="grid"><thead><tr>{thead}</tr></thead>'
            f'<tbody>{"".join(rows_html)}</tbody></table></div>')


def _b64_img(path: Path, css_class: str, alt: str) -> str:
    """Embed a PNG as a data URI so the report stays a single portable file."""
    try:
        data = base64.b64encode(path.read_bytes()).decode("ascii")
    except OSError:
        return ""
    return f'<img class="{css_class}" src="data:image/png;base64,{data}" alt="{_e(alt)}">'


def _zero_runs(depths: List[float]) -> List[Tuple[int, int]]:
    """Maximal runs of zero-coverage positions, 1-based inclusive."""
    runs: List[Tuple[int, int]] = []
    start = None
    for i, d in enumerate(depths, start=1):
        if d <= 0 and start is None:
            start = i
        elif d > 0 and start is not None:
            runs.append((start, i - 1))
            start = None
    if start is not None:
        runs.append((start, len(depths)))
    return runs


def _coverage_fig(sample: str, order: int, gene: str, ref: str,
                  depths: List[float], variants: List[Dict[str, str]],
                  expected_len: Optional[int]):
    """One segment's interactive Coverage & Variants figure (or None)."""
    try:
        import plotly.graph_objects as go
    except Exception:  # noqa: BLE001
        return None

    depths = list(depths)
    # IRMA's coverage table stops where its consensus stops, so a segment that
    # assembled short would silently plot as fully covered. Pad to the expected
    # segment length so the missing stretch is VISIBLE as zero coverage.
    if expected_len and expected_len > len(depths):
        depths += [0.0] * (expected_len - len(depths))
    total_len = len(depths)
    if not total_len:
        return None
    covered = sum(1 for d in depths if d > 0)
    pct_cov = covered / total_len * 100
    mean = sum(depths) / total_len

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=list(range(1, total_len + 1)), y=depths, mode="lines",
        line=dict(color=TEAL_DARK, width=1),
        fill="tozeroy", fillcolor="rgba(76,140,138,0.30)",
        name="depth", hovertemplate="pos %{x}: %{y:.0f}×<extra></extra>",
    ))
    for a, b in _zero_runs(depths):
        # Label only runs wide enough to carry text; every run gets the shading.
        if (b - a) > total_len * 0.02:
            fig.add_vrect(x0=a, x1=b, fillcolor="rgba(196,106,106,0.20)", line_width=0,
                          annotation_text="no coverage", annotation_position="top left",
                          annotation_font_size=10, annotation_font_color=DANGER)
        else:
            fig.add_vrect(x0=a, x1=b, fillcolor="rgba(196,106,106,0.20)", line_width=0)
    fig.add_hline(y=QC_DEPTH_FLOOR, line_dash="dash", line_color=DANGER,
                  annotation_text=f"{QC_DEPTH_FLOOR}×",
                  annotation_position="top left",
                  annotation_font_color=DANGER)
    if variants:
        vx: List[int] = []
        vy: List[float] = []
        vtext: List[str] = []
        for v in variants:
            try:
                p = int(v["position"])
            except (KeyError, ValueError):
                continue
            vx.append(p)
            vy.append(depths[p - 1] if 0 < p <= len(depths) else 0)
            vtext.append(f"{v.get('consensus') or '?'}→{v.get('minority') or '?'} "
                         f"{v.get('freq') or ''} ({v.get('count') or '?'} reads)")
        if vx:
            fig.add_trace(go.Scatter(
                x=vx, y=vy, mode="markers", name="minority SNPs",
                marker=dict(color=DANGER, size=9, symbol="diamond",
                            line=dict(color="#7E3B3B", width=1)),
                text=vtext, hovertemplate="pos %{x}: %{text}<extra>SNP</extra>",
            ))
    seg_txt = f"segment {order} · " if order != 99 else ""
    fig.update_layout(
        title=dict(text=(f"{sample} — {seg_txt}{gene} ({ref}) — "
                         f"Coverage &amp; Variants ({len(variants)} SNPs)"
                         f"<br><sup>Mean {mean:.1f}× · {total_len:,} bp · "
                         f"{pct_cov:.1f}% covered</sup>"),
                   font=dict(size=15, color=INK)),
        template="plotly_white",
        height=340,
        margin=dict(l=64, r=24, t=76, b=48),
        xaxis_title="Position (bp)",
        yaxis_title="Coverage depth",
        showlegend=False,
    )
    return fig


def _collect_segments(outdir: Path, asm: Dict[str, Any]):
    """(order, gene, ref, depths, variants, expected_len) per assembled segment."""
    tables_dir = outdir / "irma" / "tables"
    cov_tables = sorted(tables_dir.glob("*-coverage.txt")) if tables_dir.is_dir() else []
    try:
        import metadata as meta_mod
        seg_num = meta_mod.SEGMENT_NUMBER
    except Exception:  # noqa: BLE001
        seg_num = {}
    expected = {}
    for s in asm.get("segments", []) or []:
        if s.get("gene"):
            expected[str(s["gene"]).upper()] = s.get("expected_length")
    entries = []
    for tbl in cov_tables:
        ref, depths = _read_cov_depths(tbl)
        if not ref:
            ref = tbl.name.replace("-coverage.txt", "")
        gene = _cov_gene(ref)
        variants = _read_variants(tables_dir / f"{ref}-variants.txt")
        try:
            exp_len = int(expected.get(gene) or 0) or None
        except (TypeError, ValueError):
            exp_len = None
        entries.append((seg_num.get(gene, 99), gene, ref, depths, variants, exp_len))
    entries.sort(key=lambda e: (e[0], e[2]))
    return entries


_CSS = """
* { box-sizing: border-box; }
body { font-family: -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif;
       color: %(ink)s; margin: 0; padding: 24px; font-size: 13px; line-height: 1.45;
       max-width: 1100px; margin-left: auto; margin-right: auto; }
h1 { font-size: 22px; margin: 0 0 2px; }
h2 { color: %(teal)s; font-size: 15px; margin: 20px 0 6px; border-bottom: 2px solid %(border)s; padding-bottom: 3px; }
h3 { font-size: 13px; margin: 14px 0 4px; }
.sub { color: %(muted)s; margin: 0 0 12px; }
.banner { background: %(teal)s; color: #fff; padding: 10px 14px; border-radius: 6px; font-weight: 600; margin: 6px 0; }
.banner.danger { background: %(danger)s; }
.banner.warn { background: %(warn)s; }
.banner.success { background: %(success)s; }
.pill { display: inline-block; padding: 1px 8px; border-radius: 10px; color: #fff; font-size: 11px; font-weight: 700; }
table { border-collapse: collapse; width: 100%%; margin: 4px 0 8px; }
table.kv th { text-align: left; width: 34%%; background: #FBFAF8; }
table.kv th, table.kv td { border-bottom: 1px solid %(border)s; padding: 4px 8px; vertical-align: top; }
table.grid { font-size: 11.5px; }
table.grid th { background: %(teal)s; color: #fff; text-align: left; padding: 5px 6px; }
table.grid td { border-bottom: 1px solid %(border)s; padding: 4px 6px; }
table.grid tbody tr:nth-child(even) { background: #F6F5F2; }
.muted { color: %(muted)s; }
.small { color: %(muted)s; font-size: 11px; }
.scroll { overflow-x: auto; }
.figure { margin: 6px 0 10px; }
.fig-static { max-width: 100%%; height: auto; display: block; }
/* Interactive charts for browsers; their static PNG twins only exist for
   print media — which is exactly what WeasyPrint renders the PDF with. */
.fig-print { display: none; max-width: 100%%; height: auto; }
@media print {
  .fig-interactive { display: none; }
  .fig-print { display: block; }
  .no-print { display: none; }
  body { padding: 0; font-size: 11px; }
  h2 { break-after: avoid; }
  table, .figure { break-inside: avoid; }
}
@page { size: letter; margin: 14mm; }
""" % {"ink": INK, "teal": TEAL, "muted": MUTED, "border": BORDER,
       "danger": DANGER, "warn": WARN, "success": SUCCESS}


def write_html(ctx: Dict[str, Any], path: Path, outdir: Path) -> None:
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

    verdict = (asm.get("overall_verdict") or "fail").upper()
    vcolor = _VERDICT_COLOR.get(verdict, MUTED)
    geno = genoflu.get("genotype") or "(not assigned)"
    strain = _strain(rec, subtype)

    parts: List[str] = []
    parts.append("<h1>IRMA Influenza/Respiratory Virus Assembly Report</h1>")
    parts.append(f'<p class="sub">Sample <b>{_e(sample)}</b> · {_e(ctx.get("date"))} · '
                 f'IRMA module {_e(opts.get("module", "?"))} · IRMA {_e(vers.get("irma", "?"))}</p>')

    parts.append(
        f'<div class="banner" style="background:{vcolor}">Subtype {_e(subtype)} · '
        f'{_e(asm.get("segment_count", 0))} segment(s) · GenoFLU genotype: {_e(geno)} · '
        f'assembly QC: {_e(verdict)}</div>'
    )
    if cleavage.get("multibasic"):
        parts.append('<div class="banner danger">⚠ HA multibasic cleavage motif detected — '
                     'molecular indicator consistent with high-pathogenicity avian influenza (HPAI). '
                     'Confirm by reference method.</div>')

    # --- Analysis summary ---
    parts.append("<h2>Analysis summary</h2>")
    summary = (
        f"Reads were assembled with CDC IRMA ({_e(opts.get('module', 'FLU'))} module), which iteratively "
        f"gathers reads and refines a per-segment consensus. This sample assembled "
        f"<b>{_e(asm.get('segment_count', 0))}</b> segment(s) with predicted subtype <b>{_e(subtype)}</b>. "
    )
    if geno and geno != "(not assigned)":
        summary += f"GenoFLU assigned genotype <b>{_e(geno)}</b> from the per-segment lineage pattern. "
    summary += (f"The assembled segments were re-headed into a submission FASTA under the strain "
                f"name <b>{_e(strain)}</b>. Overall assembly QC verdict: <b>{_e(verdict)}</b> "
                f"(PASS requires ≥10X mean depth, &lt;10% positions below 10X, ≤2% zero-coverage per segment).")
    parts.append(f"<p>{summary}</p>")

    # --- Input file quality ---
    parts.append("<h2>Input file quality</h2>")
    files = fq.get("files") or {}
    if files:
        parts.append('<p>Per-FASTQ read statistics from <i>seqkit stats</i> (ISO 20397-2 read metrics). '
                     'Q20/Q30 are the percentage of bases at or above those Phred quality scores; '
                     'higher is better.</p>')
        body = []
        for tag in ("R1", "R2"):
            s = files.get(tag)
            if not s:
                continue
            body.append([tag, _fmt_int(s.get("num_seqs")), _fmt_int(s.get("sum_len")),
                         _fmt_int(s.get("avg_len")), _fmt_f(s.get("gc_pct")),
                         _fmt_f(s.get("q20_pct")), _fmt_f(s.get("q30_pct")),
                         str(s.get("avg_qual", "—"))])
        parts.append(_grid_html(["File", "Reads", "Bases (bp)", "Avg len", "GC%", "Q20%", "Q30%", "Avg Q"], body))
    else:
        parts.append('<p class="muted">No read-quality metrics available (seqkit unavailable).</p>')

    # --- Assembly & per-segment coverage ---
    parts.append("<h2>Assembly &amp; per-segment coverage</h2>")
    depth_png = assets / "depth.png"
    if _depth_bar(segments, depth_png):
        parts.append(f'<div class="figure">{_b64_img(depth_png, "fig-static", "Per-segment mean depth")}</div>')
    if segments:
        parts.append("<p>Each segment's consensus length, mean depth, the fraction of positions below 10X, "
                     "the fraction with zero coverage, and the QC verdict.</p>")
        body = [[s.get("gene", ""), s.get("reference_name", ""), _fmt_int(s.get("length")),
                 f"{_fmt_f(s.get('avg_depth'))}X", _fmt_f(s.get("pct_lt10x")),
                 _fmt_f(s.get("pct_zero_cov")), str(s.get("verdict", ""))] for s in segments]
        parts.append(_grid_html(["Segment", "Ref", "Length", "Mean depth", "% < 10X", "% zero", "QC"],
                                body, verdict_col=6))
    else:
        parts.append('<div class="banner danger">No segments assembled — IRMA produced no consensus '
                     'for this sample.</div>')

    # --- GenoFLU ---
    parts.append("<h2>GenoFLU genotype</h2>")
    parts.append(_kv_rows([
        ("Genotype", geno),
        ("Segments matched", _fmt_int(genoflu.get("segments_matched"))),
        ("Complete", "yes" if genoflu.get("complete") else "no"),
    ]))
    gsegs = genoflu.get("segments") or []
    if gsegs:
        body = [[s.get("segment", ""), s.get("lineage", "") or "—",
                 s.get("percent_identity", "") or "—", s.get("mismatches", "") or "—"] for s in gsegs]
        parts.append(_grid_html(["Segment", "Lineage", "% identity", "Mismatches"], body))

    # --- HA cleavage ---
    if cleavage:
        parts.append("<h2>HA cleavage site</h2>")
        parts.append(_kv_rows([
            ("Cleavage motif (HA1 | HA2)", cleavage.get("motif") or "not determined"),
            ("Multibasic (HPAI indicator)",
             "yes" if cleavage.get("multibasic") else ("no" if cleavage.get("multibasic") is False else "—")),
            ("Note", cleavage.get("note") or "—"),
        ]))

    # --- Submission sequence ---
    parts.append("<h2>Submission sequence</h2>")
    parts.append(_kv_rows([
        ("Strain name", strain),
        ("Organism", rec.get("organism") or "Influenza A virus"),
        ("Host/species", rec.get("host") or "—"),
        ("State/location", rec.get("state") or "—"),
        ("Collection year", rec.get("collection_year") or "—"),
        ("Submission FASTA", Path(man.get("submission_fasta") or "—").name),
    ]))

    # --- Methods & provenance ---
    parts.append("<h2>Methods &amp; provenance</h2>")
    iso = ", ".join(r.get("standard", "") for r in (man.get("iso_references") or []) if r.get("standard"))
    parts.append(_kv_rows([
        ("IRMA", f"{vers.get('irma', '—')} (module {opts.get('module', '—')})"),
        ("samtools / seqkit", f"{vers.get('samtools', '—')} / {vers.get('seqkit', '—')}"),
        ("Metadata source", Path(man.get("metadata_source") or "—").name),
        ("Standards referenced", iso or "—"),
    ]))
    parts.append('<p class="small">Disclaimer: subtype, genotype and cleavage-motif calls are genotypic '
                 'predictions from a consensus assembly and curated reference databases. They support, but '
                 'do not replace, confirmatory testing. HPAI determination must follow the responsible '
                 'animal-health authority\'s validated procedures (WOAH Terrestrial Manual 3.3.4). Submit '
                 'sequences under the strain name shown above only after metadata review.</p>')

    # --- Per-segment Coverage & Variants (interactive) ---
    entries = _collect_segments(outdir, asm)
    if entries:
        parts.append("<h2>Per-segment coverage &amp; SNPs</h2>")
        parts.append('<p>Per-position read depth for each assembled segment, from IRMA\'s coverage tables, '
                     'padded to the segment\'s expected length so missing stretches show as zero. '
                     'Red diamonds are the minority-variant SNPs IRMA called for that segment (hover for '
                     'the alleles, frequency and read count); shaded red spans have <b>no coverage</b>; the '
                     f'dashed line is the {QC_DEPTH_FLOOR}X QC floor. Drag to zoom into any region '
                     '(double-click resets); the toolbar saves a PNG snapshot.</p>')
        plotly_included = False
        for order, gene, ref, depths, variants, exp_len in entries:
            fig = _coverage_fig(sample, order, gene, ref, depths, variants, exp_len)
            png = assets / f"cov_{ref}.png"
            png_ok = _coverage_plot(ref, depths, png)
            if fig is not None:
                fig_html = fig.to_html(
                    full_html=False,
                    include_plotlyjs=(True if not plotly_included else False),
                    config={"responsive": True, "displaylogo": False},
                )
                plotly_included = True
                fallback = _b64_img(png, "fig-print", f"Coverage plot {ref}") if png_ok else ""
                # The interactive chart titles itself; the static print twin
                # does not — give the PDF a heading so a plot can be told
                # apart from its neighbours without reading the SNP table.
                seg_txt = f"segment {order} · " if order != 99 else ""
                print_h3 = (f'<h3 class="fig-print">{_e(sample)} — {_e(seg_txt + gene)} ({_e(ref)}) — '
                            f'Coverage &amp; Variants ({len(variants)} SNPs)</h3>')
                parts.append(f'<div class="figure">{print_h3}'
                             f'<div class="fig-interactive">{fig_html}</div>{fallback}</div>')
            elif png_ok:
                seg_txt = f"segment {order} · " if order != 99 else ""
                parts.append(f"<h3>{_e(sample)} — {_e(seg_txt + gene)} ({_e(ref)}) — "
                             f"Coverage &amp; Variants ({len(variants)} SNPs)</h3>")
                parts.append(f'<div class="figure">{_b64_img(png, "fig-static", f"Coverage plot {ref}")}</div>')
            if variants:
                body = [[v["position"], v["consensus"] or "—", v["minority"] or "—",
                         v["freq"] or "—", v["count"] or "—"] for v in variants]
                parts.append(f'<p class="small">SNPs — {_e(gene)} ({len(variants)})</p>')
                parts.append(_grid_html(["Position", "Consensus", "Minority", "Minority freq",
                                         "Minority count"], body))
            else:
                parts.append(f'<p class="small">SNPs — {_e(gene)}: none — no minority variants called '
                             'for this segment.</p>')

    doc = (f'<!doctype html><html><head><meta charset="utf-8">'
           f'<meta name="viewport" content="width=device-width, initial-scale=1">'
           f'<title>IRMA report — {_e(sample)}</title><style>{_CSS}</style></head>'
           f'<body>{"".join(parts)}</body></html>')
    Path(path).write_text(doc, encoding="utf-8")


def html_to_pdf(html_path: Path, pdf_path: Path) -> bool:
    """Render report.html to report.pdf via WeasyPrint (print media: the static
    figure twins). True on success; False when WeasyPrint or its native libs
    are unavailable — callers fall back to the reportlab PDF."""
    try:
        from weasyprint import HTML
    except Exception:  # noqa: BLE001 — import may fail on missing native libs
        return False
    try:
        HTML(filename=str(html_path)).write_pdf(str(pdf_path))
        return True
    except Exception:  # noqa: BLE001
        return False
