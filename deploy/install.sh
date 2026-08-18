#!/usr/bin/env bash
# install.sh — idempotent, no-sudo deployment of the IRMA GUI.
#
# Mirrors the sibling tools' install pattern. Every heavy step is skippable and
# clearly logged. Safe to re-run. Designed to be portable to other OOD systems:
# the only host assumption is a conda/miniforge base (pass --conda-base).
#
# What it does:
#   1. Locate/create the conda env (shared at <repo>/env, else personal irma_gui).
#   2. pip install backend/requirements.txt into that env.
#   3. Verify IRMA (and its bundled FLU/CoV reference modules).
#   4. Verify GenoFLU (from the sibling genoflu_gui env) + its reference set;
#      optionally stage the set to the shared databases dir so it survives
#      env rebuilds.
#   5. Build the React frontend (frontend/dist/).
#
# Usage:
#   deploy/install.sh [--personal] [--conda-base DIR]
#                     [--skip-genoflu-db] [--skip-frontend] [--dry-run]
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

SHARED_ENV="${REPO_DIR}/env"
PERSONAL_ENV_NAME="irma_gui"
CONDA_BASE="${HOME}/miniforge3"
USE_PERSONAL=0
SKIP_GENOFLU_DB=0
SKIP_FRONTEND=0
DRY_RUN=0
# Optional: stage GenoFLU's bundled reference set here so config.py's genoflu_db
# can point at a stable, env-independent copy. Empty by default (use bundled).
GENOFLU_DB_DEST="${GENOFLU_DB_DEST:-/srv/kapurlab/databases/genoflu}"

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m  ok\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m  !!\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mERROR\033[0m %s\n' "$*" >&2; exit 1; }
run()  { if [[ ${DRY_RUN} -eq 1 ]]; then echo "  [dry-run] $*"; else "$@"; fi; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --personal)         USE_PERSONAL=1; shift;;
    --conda-base)       CONDA_BASE="$2"; shift 2;;
    --skip-genoflu-db)  SKIP_GENOFLU_DB=1; shift;;
    --skip-frontend)    SKIP_FRONTEND=1; shift;;
    --dry-run)          DRY_RUN=1; shift;;
    -h|--help)          sed -n '2,30p' "$0"; exit 0;;
    *) die "unknown arg: $1";;
  esac
done

log "IRMA GUI install"
echo "  repo:  ${REPO_DIR}"
[[ ${DRY_RUN} -eq 1 ]] && warn "DRY RUN — no changes will be made"

# ---------------------------------------------------------------------------
# 1. conda env
# ---------------------------------------------------------------------------
CONDA="${CONDA_BASE}/bin/conda"
[[ -x "${CONDA}" ]] || CONDA="$(command -v conda 2>/dev/null || true)"
[[ -n "${CONDA}" && -x "${CONDA}" ]] || die "conda not found. Install miniforge to ${CONDA_BASE} or pass --conda-base."
ok "conda: ${CONDA}"
CONDA_FRONTEND="${CONDA_FRONTEND:-}"
if [[ -z "${CONDA_FRONTEND}" ]]; then
  if [[ -x "${CONDA_BASE}/bin/mamba" ]]; then CONDA_FRONTEND="${CONDA_BASE}/bin/mamba"
  elif command -v mamba >/dev/null 2>&1; then CONDA_FRONTEND="$(command -v mamba)"
  else CONDA_FRONTEND="${CONDA}"; fi
fi
ok "env builder: ${CONDA_FRONTEND}"

ENV_FILE="${REPO_DIR}/conda_setup/environment.yml"
if [[ ${USE_PERSONAL} -eq 1 ]]; then
  ENV_BIN="$("${CONDA}" run -n "${PERSONAL_ENV_NAME}" sh -c 'echo $CONDA_PREFIX/bin' 2>/dev/null || true)"
  ENV_DESC="personal env ${PERSONAL_ENV_NAME}"
  ENV_EXISTS=$("${CONDA}" env list | awk '{print $1}' | grep -qx "${PERSONAL_ENV_NAME}" && echo 1 || echo 0)
  CREATE_FLAG=("-n" "${PERSONAL_ENV_NAME}")
else
  ENV_BIN="${SHARED_ENV}/bin"
  ENV_DESC="shared env ${SHARED_ENV}"
  ENV_EXISTS=$([[ -x "${SHARED_ENV}/bin/python" ]] && echo 1 || echo 0)
  CREATE_FLAG=("-p" "${SHARED_ENV}")
fi

if [[ "${ENV_EXISTS}" -eq 1 ]]; then
  ok "${ENV_DESC} already exists — skipping create"
else
  # A cancelled solve can leave a partial env dir; clear it first.
  if [[ ${USE_PERSONAL} -eq 0 && -d "${SHARED_ENV}" ]]; then
    warn "removing incomplete env at ${SHARED_ENV} (no python found)"
    run rm -rf "${SHARED_ENV}"
  fi
  log "creating ${ENV_DESC} from ${ENV_FILE} (solve can take 3-8 min)"
  run "${CONDA_FRONTEND}" env create "${CREATE_FLAG[@]}" -f "${ENV_FILE}"
fi

# A --personal env may have just been created above; if so, the ENV_BIN probed
# earlier (via `conda run` before the env existed) is empty, which would make
# PYTHON="/python". Re-resolve now that the env exists — prefer the live prefix,
# fall back to <conda base>/envs/<name> (where `conda env create -n` puts it).
if [[ ${USE_PERSONAL} -eq 1 && ! -x "${ENV_BIN}/python" ]]; then
  ENV_BIN="$("${CONDA}" run -n "${PERSONAL_ENV_NAME}" sh -c 'echo $CONDA_PREFIX/bin' 2>/dev/null || true)"
  [[ -x "${ENV_BIN}/python" ]] || ENV_BIN="$("${CONDA}" info --base 2>/dev/null)/envs/${PERSONAL_ENV_NAME}/bin"
fi
PYTHON="${ENV_BIN}/python"
[[ ${DRY_RUN} -eq 1 || -x "${PYTHON}" ]] || die "env python not found at '${PYTHON}' — ${ENV_DESC} did not build correctly."
# Put the env's bin on PATH for every tool call below: IRMA, seqkit, samtools.
# (GenoFLU and the blastn/makeblastdb it shells out to run from the sibling
# genoflu_gui env — see section 3.) The OOD session sets PATH the same way.
if [[ -d "${ENV_BIN}" ]]; then export PATH="${ENV_BIN}:${PATH}"; fi
log "pip install backend requirements into ${ENV_DESC}"
run "${PYTHON}" -m pip install -r "${REPO_DIR}/backend/requirements.txt"

# ---------------------------------------------------------------------------
# 2. Verify IRMA
# ---------------------------------------------------------------------------
if command -v IRMA >/dev/null 2>&1; then
  ok "IRMA on PATH: $(command -v IRMA)"
  # IRMA prints its banner/modules with no args; show the first lines.
  IRMA 2>&1 | head -3 || true
else
  warn "IRMA not found in env — assembly will fail at runtime. Check the env build."
fi

# ---------------------------------------------------------------------------
# 3. Verify GenoFLU + reference set (optionally stage to shared databases)
# ---------------------------------------------------------------------------
# GenoFLU runs from genoflu_gui's own env at runtime (bin/run_genoflu.py,
# _sibling_env_dir) — a copy in this env would tie the two solves together and
# could drift from the suite's pinned version. Resolve it the way the runner
# will: sibling env first, then PATH (envs built before the split).
GENOFLU=""
for _cand in "${BDTOOLS_SIBLING_ENV_GENOFLU_GUI:-}" \
             "$(dirname "${REPO_DIR}")/genoflu_gui/env" \
             "${BDTOOLS_HOME:-${XDG_DATA_HOME:-${HOME}/.local/share}/bdtools}/checkouts/genoflu_gui/env"; do
  if [[ -n "${_cand}" && -x "${_cand}/bin/genoflu.py" ]]; then
    GENOFLU="${_cand}/bin/genoflu.py"
    break
  fi
done
[[ -n "${GENOFLU}" ]] || GENOFLU="$(command -v genoflu.py || command -v genoflu || true)"
if [[ -n "${GENOFLU}" ]]; then
  ok "GenoFLU: ${GENOFLU}"
  # Bundled reference set lives at <script>/../dependencies (fastas + key).
  GENOFLU_DEP="$(dirname "$(dirname "$(readlink -f "${GENOFLU}")")")/dependencies"
  if [[ -d "${GENOFLU_DEP}/fastas" && -f "${GENOFLU_DEP}/genotype_key.xlsx" ]]; then
    ok "GenoFLU reference set present: ${GENOFLU_DEP}"
    if [[ ${SKIP_GENOFLU_DB} -eq 0 ]] && mkdir -p "${GENOFLU_DB_DEST}" 2>/dev/null && [[ -w "${GENOFLU_DB_DEST}" ]]; then
      if [[ ! -d "${GENOFLU_DB_DEST}/dependencies/fastas" ]]; then
        log "staging GenoFLU reference set to ${GENOFLU_DB_DEST}/dependencies (survives env rebuilds)"
        run cp -a "${GENOFLU_DEP}" "${GENOFLU_DB_DEST}/dependencies"
      else
        ok "shared GenoFLU reference set already staged: ${GENOFLU_DB_DEST}/dependencies"
      fi
    fi
  else
    warn "GenoFLU bundled reference set not found at ${GENOFLU_DEP}; GenoFLU will be skipped."
  fi
else
  warn "GenoFLU not found (no sibling genoflu_gui env, not on PATH) — genotyping will be skipped at runtime."
fi

# seqkit / samtools sanity
command -v seqkit  >/dev/null 2>&1 && ok "seqkit: $(seqkit version 2>&1 | head -1)"   || warn "seqkit missing"
command -v samtools >/dev/null 2>&1 && ok "samtools present"                            || warn "samtools missing"

# ---------------------------------------------------------------------------
# 4. Frontend build
# ---------------------------------------------------------------------------
if [[ ${SKIP_FRONTEND} -eq 1 ]]; then
  warn "skipping frontend build (--skip-frontend)"
else
  log "building React frontend"
  pushd "${REPO_DIR}/frontend" >/dev/null
  if command -v npm >/dev/null 2>&1; then
    run npm ci || run npm install
    run npm run build
  elif [[ -x node_modules/.bin/vite ]]; then
    run node_modules/.bin/vite build
  else
    SIB="/srv/kapurlab/tools/amr_plus_gui/frontend/node_modules"
    if [[ -d "${SIB}" && ! -e node_modules ]]; then
      run ln -s "${SIB}" node_modules
      run node_modules/.bin/vite build
    else
      warn "no npm and no node_modules — frontend not built. Install Node and re-run."
    fi
  fi
  popd >/dev/null
  [[ -f "${REPO_DIR}/frontend/dist/index.html" ]] && ok "frontend built: ${REPO_DIR}/frontend/dist/"
fi

log "Done. Register the OOD apps (sudo deploy/register_ood_apps.sh) and launch a session."
echo "  Backend entry:  ${REPO_DIR}/backend/app/main.py (uvicorn app.main:app)"
echo "  Env python:     ${PYTHON}"
