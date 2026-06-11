# IRMA GUI

A web interface for **CDC IRMA** influenza / SARS-CoV-2 genome assembly, part of
the Kapur Lab Open OnDemand pipeline family (`vsnp_gui`, `kraken_id_parse_gui`,
`amr_plus_gui`, `mlst_gui`, `genoflu_gui`).

From raw reads, per sample, it:

1. runs **read QC** (`seqkit`, Q20/Q30 — ISO 20397-2 metrics),
2. assembles segments with **CDC IRMA** (FLU or CoV module),
3. summarizes **per-segment coverage** + predicts the influenza **subtype**,
4. rebuilds the segment headers into a **`<sample>-submission.fasta`** using
   per-sample metadata (the GenBank/GISAID strain name),
5. genotypes the influenza genome with **GenoFLU**,
6. produces a **PDF report** and a **single-labeled-column Excel** of stats
   (modelled on the vSNP3 workbook).

## Sample metadata → submission headers

Metadata is matched on the **sample name** and managed two interchangeable ways:

- a **web table** in the GUI (Sample metadata panel), and
- an **Excel** workbook you can **Download**, edit locally, and **Replace**.

Fields: `sample, organism, host, state, collection_year, passage, strain`. The
pipeline builds `A/host/state/sample/year(HxNx)` (or uses a `strain` override)
and writes it into every segment defline of the submission FASTA.

## Install

```bash
deploy/install.sh --conda-base /srv/kapurlab/tools/miniforge3   # env + frontend
sudo deploy/register_ood_apps.sh                                 # dashboard apps (root)
```

`install.sh` is idempotent and supports `--dry-run`, `--personal`,
`--skip-frontend`, `--skip-genoflu-db`. IRMA and GenoFLU ship their reference
data inside the conda packages, so there is no separate database download for
the core pipeline.

## OOD apps

- **IRMA** — production: serves the committed `frontend/dist/` from the shared env.
- **IRMA (dev)** — branch-picker: checks out a chosen git branch into a
  per-session worktree and rebuilds the frontend, for testing before merge.

## Run from the CLI

```bash
export PATH=/srv/kapurlab/tools/irma_gui/env/bin:$PATH
export PYTHONPATH=/srv/kapurlab/tools/irma_gui/bin
python bin/irma_pipeline.py --sample S --outdir OUT -r1 R1.fastq.gz -r2 R2.fastq.gz --module FLU
```

See `CLAUDE.md` for the full architecture and conventions.
