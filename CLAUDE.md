# IRMA GUI — Claude Code Context

> Read this before editing. This is one of the Kapur Lab OOD pipeline tools
> (siblings: `vsnp_gui`, `kraken_id_parse_gui`, `amr_plus_gui`, `mlst_gui`,
> `genoflu_gui`). The shared conventions + gotchas live in
> `/srv/kapurlab/tools/amr_plus_gui/docs/BUILDING_A_SIBLING_TOOL.md` — read that too.

## What this is

A web GUI for **CDC IRMA** (Iterative Refinement Meta-Assembler,
https://github.com/CDCgov/irma) — reference-free, iterative assembly of
segmented RNA-virus genomes (**influenza A/B** via the FLU module, **SARS-CoV-2**
via CoV) directly from reads. Beyond assembly, this tool:

1. **Builds a submission FASTA** — re-headers the assembled segments with the
   influenza strain name (`A/host/state/sample/year(HxNx)`) from per-sample
   metadata, the deliverable a lab submits to GenBank/GISAID.
2. **Captures metadata two ways**, matched on the sample name: a web table and
   an Excel workbook (`Download` / `Replace`), both stored in `<project>/irma/`.
3. **Genotypes with GenoFLU** — runs USDA-VS GenoFLU on the assembled influenza
   genome and reports the genotype.
4. **Reports** — a PDF (input QC, summary, per-segment coverage with figures,
   genotype, HA cleavage, methods/provenance) and a single-labeled-column
   stats Excel modelled on the vSNP3 workbook.

FastAPI backend + React (Vite) SPA, deployed as an **Open OnDemand
batch_connect** app. One uvicorn per user session behind OOD's Apache proxy at
`/rnode/<host>/<port>/`. FastAPI serves `frontend/dist/`. The pipeline runs as a
background subprocess tracked by `JobManager`, logs streamed over SSE.

## Layout

```
/srv/kapurlab/tools/irma_gui/
  backend/app/
    main.py        all FastAPI routes (projects, metadata, run, jobs, irma-results/irma-table)
    config.py      load/save per-user ~/.config/irma_gui/config.json
    jobs.py        JobManager (PID marker "irma_pipeline"); reused from siblings
    sra.py         SRA accession helpers (reused)
  bin/
    irma_pipeline.py  orchestrator: read QC -> IRMA -> coverage -> submission FASTA -> GenoFLU -> report
    run_irma.py       IRMA runner + run_manifest.json provenance (ISO refs)
    run_genoflu.py    GenoFLU runner (reused from genoflu_gui; bundled refs)
    metadata.py       per-sample metadata (web JSON + Excel) + submission deflines
    reporting/        stats_excel.py (single-column xlsx) + pdf_report.py
  conda_setup/environment.yml   env name irma_gui (irma, genoflu, blast, seqkit, samtools, web+report deps)
  deploy/install.sh             idempotent env+frontend install (portable, no sudo)
  deploy/register_ood_apps.sh   copy ood/apps/* into /var/www/ood/apps/sys (root)
  ood/apps/irma_gui{,_dev}/     OOD app definitions (prod + dev branch-picker)
  frontend/src/App.jsx          the SPA; App.css is the shared theme (verbatim)
```

## Hard constraints (break -> silent failure)

1. **All frontend URLs relative** (`fetch("./api/...")`, `new EventSource("./api/jobs/<id>/log")`).
   `vite.config.js` keeps `base: "./"`. The browser origin is the OOD server, not the app.
2. **FastAPI serves the SPA** from `frontend/dist/`. No separate static server.
3. **Rebuild the frontend after any `frontend/src` edit** (`npm run build`); uvicorn serves `dist/`.
4. **Use the env Python** `/srv/kapurlab/tools/irma_gui/env/bin/python`, never system/base.
5. **`<env>/bin` must be on PATH** when the pipeline runs — IRMA, and blastn/makeblastdb
   (GenoFLU shells out to them), seqkit and samtools all resolve there. The OOD
   `script.sh.erb` and `install.sh` set this. (No `$CONDA_PREFIX` needed.)

## How a run works

`POST /api/run {project, r1, r2?, module?, run_genoflu?, genoflu_pident?, header_style?, metadata_xlsx?}` ->
`bin/irma_pipeline.py --sample S --outdir <project>/irma/S -r1 R1 [-r2 R2] --module FLU`:
1. **seqkit** read QC -> `fastq_qc.json` (reads, length, GC, Q20/Q30 — ISO 20397-2 metrics).
2. **IRMA** (`run_irma.py`) -> `<run>/irma/` (per-segment consensus + tables/figures),
   `run_manifest.json` (every option, tool versions, ISO refs).
3. **Assembly + coverage** -> `assembly.fasta` (concatenated consensus) + `assembly_stats.json`
   (per-segment depth / %<10X / %zero-coverage + subtype H/N + QC verdict).
4. **Submission FASTA** -> `<sample>-submission.fasta` with strain-name deflines from
   metadata (`metadata.py`, matched on sample name).
5. **HA cleavage** (influenza) -> `ha_cleavage.json` (multibasic = HPAI indicator).
6. **GenoFLU** (`run_genoflu.py`) on the assembled genome -> `genoflu_result.json`.
7. **Report** -> `<sample>_<date>_stats.xlsx` (single labeled column) + `report.pdf`.

Per-sample results are read straight off disk from `<project>/irma/<sample>/`, so any past run
is revisitable. Result categories/labels + media handling live in `main.py`.

## Metadata & submission headers

Canonical store: `<project>/irma/sample_metadata.json` (web table edits it).
Mirror workbook: `<project>/irma/sample_metadata.xlsx` (Download / Replace).
Fields: `sample, organism, host, state, collection_year, passage, strain`. The
pipeline resolves a sample's record (external `--metadata-xlsx` wins, else the
JSON), builds `A/host/state/sample/year(HxNx)` (or the `strain` override), and
writes one record per segment into the submission FASTA. `metadata.py` is the
single source of truth, imported by both the pipeline and the backend.

## Databases / references (no separate download for the core pipeline)

- **IRMA** ships its FLU and CoV reference modules inside the conda package.
- **GenoFLU** ships its reference set (`dependencies/fastas` + `genotype_key.xlsx`)
  inside the conda package; `run_genoflu.py` resolves it relative to `genoflu.py`.
  `config.py`'s `genoflu_db` overrides it only to pin an out-of-tree set; `install.sh`
  can stage a copy to `/srv/kapurlab/databases/genoflu/dependencies`.

## Reloads (what picks up an edit)

- `bin/` scripts -> next pipeline run (subprocess reads from disk).
- `backend/app/` -> new OOD session (or `--reload` in the dev app).
- `frontend/src` -> `npm run build`, then a new session.
- `ood/**` -> re-run `sudo deploy/register_ood_apps.sh` (the registered copy is a snapshot).

## Key paths

| Item | Path |
|---|---|
| Env Python | `/srv/kapurlab/tools/irma_gui/env/bin/python` |
| IRMA | `<env>/bin/IRMA` |
| GenoFLU | `<env>/bin/genoflu.py` (+ `<env>/dependencies/`) |
| Shared projects | `/srv/kapurlab/projects/` |
| OOD app (deployed) | `/var/www/ood/apps/sys/irma_gui{,_dev}` |
| OOD app (source) | `/srv/kapurlab/tools/irma_gui/ood/apps/` <- edit here |
