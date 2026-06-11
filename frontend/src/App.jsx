import { useState, useEffect, useRef } from "react";
import "./App.css";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------
const APP_VERSION = "0.1.0";

const IRMA_MODULES = ["FLU", "CoV"];
const HEADER_STYLES = ["ncbi", "strain"];

function fileIcon(name) {
  if (name.endsWith(".json")) return "📁";
  if (name.endsWith(".tsv")) return "📊";
  if (name.endsWith(".xlsx")) return "📊";
  if (name.endsWith(".pdf")) return "📄";
  if (name.endsWith(".png")) return "🖼";
  if (name.endsWith(".fasta") || name.endsWith(".fa")) return "🧬";
  if (name.endsWith(".txt")) return "📝";
  if (name.endsWith(".html")) return "🌐";
  return "📁";
}

function fmtSize(bytes) {
  if (bytes == null) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

// Color the QC verdict badge
function verdictClass(verdict) {
  const v = (verdict || "").toUpperCase();
  if (v === "PASS") return "verdict-pass";
  if (v === "FAIL") return "verdict-fail";
  if (v === "REVIEW") return "verdict-review";
  return "";
}

// ---------------------------------------------------------------------------
// App
// ---------------------------------------------------------------------------
export default function App() {
  const [projects, setProjects] = useState([]);
  const [projectsLoading, setProjectsLoading] = useState(true);
  const [newProjectName, setNewProjectName] = useState("");
  const [creatingProject, setCreatingProject] = useState(false);
  const [activeProject, setActiveProject] = useState("");
  const [addPath, setAddPath] = useState({});
  const [sraText, setSraText] = useState({});
  const [addStatus, setAddStatus] = useState({});
  const [inputsByProj, setInputsByProj] = useState({});
  const uploadProjRef = useRef("");
  const uploadInputRef = useRef(null);
  const [expanded, setExpanded] = useState({});
  const [samples, setSamples] = useState({});
  const [checkedKeys, setCheckedKeys] = useState({});
  const [openResults, setOpenResults] = useState({});
  const [sampleResults, setSampleResults] = useState({});  // key -> {loading,status,present,files}
  const [irmaTables, setIrmaTables] = useState({});        // key -> parsed irma-table
  const [activeRun, setActiveRun] = useState(null);
  const [queueInfo, setQueueInfo] = useState({ total: 0, done: 0 });

  // IRMA run config
  const [irmaModule, setIrmaModule] = useState("FLU");
  const [runGenoflu, setRunGenoflu] = useState(true);
  const [genoFluPident, setGenoFluPident] = useState(98);
  const [genoFluDb, setGenoFluDb] = useState("");
  const [headerStyle, setHeaderStyle] = useState("ncbi");

  const [running, setRunning] = useState(false);
  const [jobId, setJobId] = useState(null);
  const [jobStatus, setJobStatus] = useState("idle");
  const [logLines, setLogLines] = useState([]);
  const [settingsDraft, setSettingsDraft] = useState({});
  const [folderBrowser, setFolderBrowser] = useState({ open: false, path: "", parent: null, entries: [], loading: false, error: "" });
  const [currentStep, setCurrentStep] = useState("");

  const [showSettings, setShowSettings] = useState(false);
  const [showProjects, setShowProjects] = useState(true);
  const [showMetadata, setShowMetadata] = useState(true);
  const [showRun, setShowRun] = useState(true);
  const [showResults, setShowResults] = useState(true);
  const [showLogs, setShowLogs] = useState(true);

  // Which sample's results the bottom Results pane shows.
  const [selectedResultKey, setSelectedResultKey] = useState(null);

  // Per-project metadata state
  const [metadataByProj, setMetadataByProj] = useState({});  // project -> { fields, samples, records, xlsx_present }
  const [metaSaving, setMetaSaving] = useState({});           // project -> bool
  const [metaImportStatus, setMetaImportStatus] = useState({});
  const metaUploadRef = useRef(null);
  const metaUploadProjRef = useRef("");

  const logRef = useRef(null);
  const eventSourceRef = useRef(null);

  useEffect(() => {
    fetch("./api/config")
      .then((r) => r.json())
      .then((cfg) => {
        setIrmaModule(cfg.irma_module || "FLU");
        setRunGenoflu(cfg.run_genoflu !== undefined ? cfg.run_genoflu : true);
        setGenoFluPident(cfg.genoflu_pident || 98);
        setGenoFluDb(cfg.genoflu_db || "");
        setSettingsDraft(cfg);
      })
      .catch(() => {});
    loadProjects();
    fetch("./api/jobs")
      .then((r) => r.json())
      .then((jobs) => {
        const live = jobs.find((j) => j.status === "running");
        if (live) {
          setJobId(live.id);
          setJobStatus("running");
          setRunning(true);
          let samp = null;
          const m = (live.name || "").match(/^(.*?)\/(.*?) — /);
          if (m) {
            samp = { project: m[1], sample: m[2] };
            setActiveRun(samp);
          }
          streamLogUntilDone(live.id, samp, () => {});
        }
      })
      .catch(() => {});
  }, []);

  function loadProjects() {
    setProjectsLoading(true);
    fetch("./api/projects")
      .then((r) => r.json())
      .then((data) => {
        setProjects(data);
        setProjectsLoading(false);
      })
      .catch(() => setProjectsLoading(false));
  }

  async function createProject() {
    const name = newProjectName.trim();
    if (!name || creatingProject) return;
    setCreatingProject(true);
    try {
      const res = await fetch("./api/projects", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
      });
      if (!res.ok) {
        const detail = await res.json().catch(() => ({}));
        window.alert(`Could not create project: ${detail.detail || res.status}`);
        return;
      }
      const created = await res.json().catch(() => ({}));
      setNewProjectName("");
      loadProjects();
      if (created.name) {
        const n = created.name;
        setExpanded((e) => ({ ...e, [n]: true }));
        setActiveProject(n);
        await Promise.all([fetchSamples(n), loadInputs(n)]);
      }
    } finally {
      setCreatingProject(false);
    }
  }

  useEffect(() => {
    const el = logRef.current;
    if (!el) return;
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
    if (nearBottom) el.scrollTop = el.scrollHeight;
  }, [logLines]);

  useEffect(() => {
    if (!projects.length) {
      if (activeProject) setActiveProject("");
      return;
    }
    if (!activeProject || !projects.find((p) => p.name === activeProject)) {
      const first = projects[0].name;
      setActiveProject(first);
      if (inputsByProj[first] === undefined) loadInputs(first);
    }
  }, [projects]);

  function fetchSamples(name) {
    return fetch(`./api/projects/${encodeURIComponent(name)}/samples`)
      .then((r) => r.json())
      .then((data) => setSamples((s) => ({ ...s, [name]: data })))
      .catch(() => setSamples((s) => ({ ...s, [name]: [] })));
  }

  function toggleProject(name) {
    const isExpanded = expanded[name];
    setExpanded((e) => ({ ...e, [name]: !isExpanded }));
    setActiveProject(name);
    if (!isExpanded) {
      if (!samples[name]) fetchSamples(name);
      loadInputs(name);
    }
  }

  function selectProject(name) {
    setActiveProject(name);
    if (inputsByProj[name] === undefined) loadInputs(name);
  }

  function loadInputs(name) {
    return fetch(`./api/projects/${encodeURIComponent(name)}/inputs`)
      .then((r) => r.json())
      .then((data) => setInputsByProj((m) => ({ ...m, [name]: data })))
      .catch(() => setInputsByProj((m) => ({ ...m, [name]: { files: [], count: 0, total_bytes: 0 } })));
  }

  const setStat = (name, msg) => setAddStatus((m) => ({ ...m, [name]: msg }));

  async function refreshAfterLoad(name) {
    await Promise.all([fetchSamples(name), loadInputs(name)]);
    loadProjects();
  }

  async function linkLocal(name) {
    const path = (addPath[name] || "").trim();
    if (!path) return;
    setStat(name, "Linking…");
    try {
      const res = await fetch(`./api/projects/${encodeURIComponent(name)}/link-local`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) { setStat(name, `Import failed: ${data.detail || res.status}`); return; }
      setStat(name, `Linked ${data.linked} file${data.linked === 1 ? "" : "s"}.`);
      setAddPath((m) => ({ ...m, [name]: "" }));
      await refreshAfterLoad(name);
    } catch (e) {
      setStat(name, `Import failed: ${e.message}`);
    }
  }

  function pickFiles(name) {
    uploadProjRef.current = name;
    uploadInputRef.current?.click();
  }

  async function uploadFiles(name, fileList) {
    const files = Array.from(fileList || []).filter(
      (f) => f.name.endsWith(".fastq.gz")
    );
    if (!name || !files.length) return;
    const fd = new FormData();
    files.forEach((f) => fd.append("files", f));
    setStat(name, `Uploading ${files.length} file${files.length === 1 ? "" : "s"}…`);
    try {
      const res = await fetch(`./api/projects/${encodeURIComponent(name)}/upload`, { method: "POST", body: fd });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) { setStat(name, `Upload failed: ${data.detail || res.status}`); return; }
      setStat(name, `Uploaded ${data.uploaded} file${data.uploaded === 1 ? "" : "s"}.`);
      await refreshAfterLoad(name);
    } catch (e) {
      setStat(name, `Upload failed: ${e.message}`);
    }
  }

  function parseAccessions(text) {
    return (text || "").split(/[\s,]+/).map((s) => s.trim()).filter(Boolean);
  }

  async function sraDownload(name) {
    const accessions = parseAccessions(sraText[name]);
    if (!accessions.length) return;
    setStat(name, `Resolving ${accessions.length} accession${accessions.length === 1 ? "" : "s"}…`);
    setShowLogs(true);
    try {
      const res = await fetch(`./api/projects/${encodeURIComponent(name)}/sra/download`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ accessions }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) { setStat(name, `Download failed: ${data.detail || res.status}`); return; }
      setStat(name, "Downloading… progress shows in the Pipeline Log below.");
      setSraText((m) => ({ ...m, [name]: "" }));
      setJobId(data.job_id);
      setJobStatus("running");
      setLogLines([]);
      streamLogUntilDone(data.job_id, null, () => {
        setStat(name, "Download finished — see samples below.");
        refreshAfterLoad(name);
      });
    } catch (e) {
      setStat(name, `Download failed: ${e.message}`);
    }
  }

  async function deleteInput(name, filename) {
    if (!window.confirm(`Remove ${filename} from this project's download/ folder?`)) return;
    try {
      await fetch(`./api/projects/${encodeURIComponent(name)}/inputs/${encodeURIComponent(filename)}`, { method: "DELETE" });
      await refreshAfterLoad(name);
    } catch (e) {
      setStat(name, `Delete failed: ${e.message}`);
    }
  }

  const sampleKey = (project, s) => `${project}::${s.sample}`;
  const isActive = (project, s) =>
    activeRun && activeRun.project === project && activeRun.sample === s.sample;

  function toggleChecked(project, s) {
    const key = sampleKey(project, s);
    setCheckedKeys((m) => {
      const next = { ...m };
      if (next[key]) delete next[key];
      else next[key] = { project, ...s };
      return next;
    });
  }

  function loadSampleResults(project, s) {
    const key = sampleKey(project, s);
    setSampleResults((m) => ({ ...m, [key]: { ...(m[key] || {}), loading: true } }));
    fetch(`./api/projects/${encodeURIComponent(project)}/samples/${encodeURIComponent(s.sample)}/irma-results`)
      .then((r) => r.json())
      .then((data) => setSampleResults((m) => ({ ...m, [key]: { loading: false, ...data } })))
      .catch(() => setSampleResults((m) => ({ ...m, [key]: { loading: false, present: false, status: "none", files: [] } })));
  }

  function loadIrmaTable(project, s) {
    const key = sampleKey(project, s);
    setIrmaTables((m) => ({ ...m, [key]: { ...(m[key] || {}), loading: true } }));
    fetch(`./api/projects/${encodeURIComponent(project)}/samples/${encodeURIComponent(s.sample)}/irma-table`)
      .then((r) => r.json())
      .then((data) => setIrmaTables((m) => ({ ...m, [key]: { loading: false, ...data } })))
      .catch(() => setIrmaTables((m) => ({ ...m, [key]: { loading: false, present: false } })));
  }

  function toggleResults(project, s) {
    const key = sampleKey(project, s);
    const willOpen = !openResults[key];
    setOpenResults((m) => ({ ...m, [key]: willOpen }));
    if (willOpen) {
      setSelectedResultKey(key);
      setShowResults(true);
      if (!sampleResults[key]) loadSampleResults(project, s);
      if (!irmaTables[key]) loadIrmaTable(project, s);
    }
  }

  async function runSamples(list) {
    if (running || !list.length) return;
    setShowLogs(true);
    setQueueInfo({ total: list.length, done: 0 });
    for (let i = 0; i < list.length; i++) {
      await runOne(list[i]);
      setQueueInfo({ total: list.length, done: i + 1 });
    }
    setActiveRun(null);
  }

  function runSelected() {
    runSamples(Object.values(checkedKeys));
  }

  function runOne(samp) {
    return new Promise((resolve) => {
      if (eventSourceRef.current) {
        eventSourceRef.current.close();
        eventSourceRef.current = null;
      }
      setRunning(true);
      setActiveRun({ project: samp.project, sample: samp.sample });
      setJobStatus("running");
      setLogLines([]);
      setCurrentStep("");
      const key = sampleKey(samp.project, samp);
      setSampleResults((m) => ({ ...m, [key]: { ...(m[key] || {}), status: "running" } }));
      setOpenResults((m) => ({ ...m, [key]: true }));
      setSelectedResultKey(key);

      fetch("./api/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          project: samp.project,
          r1: samp.r1,
          r2: samp.r2 || null,
          module: irmaModule || null,
          run_genoflu: runGenoflu,
          genoflu_pident: genoFluPident,
          genoflu_db: genoFluDb.trim() || null,
          header_style: headerStyle || null,
        }),
      })
        .then((r) => (r.ok ? r.json() : r.json().then((e) => { throw new Error(e.detail || "Run failed"); })))
        .then(({ job_id }) => {
          setJobId(job_id);
          streamLogUntilDone(job_id, samp, resolve);
        })
        .catch((err) => {
          setLogLines((prev) => [...prev, `ERROR: ${err.message}`]);
          setRunning(false);
          setJobStatus("failed");
          resolve();
        });
    });
  }

  function streamLogUntilDone(id, samp, done) {
    const es = new EventSource(`./api/jobs/${id}/log`);
    eventSourceRef.current = es;
    es.onmessage = (evt) => {
      const data = evt.data;
      if (data === "[DONE]") {
        es.close();
        setRunning(false);
        fetch(`./api/jobs/${id}`)
          .then((r) => r.json())
          .then((job) => {
            setJobStatus(job.status);
            setCurrentStep("");
            if (samp) {
              loadSampleResults(samp.project, samp);
              loadIrmaTable(samp.project, samp);
            }
            loadProjects();
          })
          .catch(() => {})
          .finally(() => done());
      } else {
        setLogLines((prev) => [...prev, data]);
        if (/Step \d+:/i.test(data) ||
            /IRMA/i.test(data) ||
            /GenoFLU/i.test(data) ||
            /Pipeline completed/i.test(data)) {
          setCurrentStep(data.trim().replace(/^#+\s*/, ""));
        }
      }
    };
    es.onerror = () => {
      es.close();
      setRunning(false);
      setJobStatus("failed");
      done();
    };
  }

  function browseDirs(path) {
    setFolderBrowser((s) => ({ ...s, loading: true, error: "" }));
    fetch(`./api/browse-dirs?path=${encodeURIComponent(path || "")}`)
      .then((r) => (r.ok ? r.json() : r.json().then((e) => { throw new Error(e.detail || "Cannot open folder"); })))
      .then((d) => setFolderBrowser((s) => ({ ...s, path: d.path, parent: d.parent, entries: d.entries, loading: false })))
      .catch((err) => setFolderBrowser((s) => ({ ...s, loading: false, error: err.message })));
  }
  function openFolderBrowser() {
    setFolderBrowser({ open: true, path: "", parent: null, entries: [], loading: true, error: "" });
    browseDirs(settingsDraft.projects_root || "");
  }
  function chooseFolder() {
    setSettingsDraft((d) => ({ ...d, projects_root: folderBrowser.path }));
    setFolderBrowser((s) => ({ ...s, open: false }));
  }

  function saveSettings() {
    fetch("./api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        irma_module: settingsDraft.irma_module,
        run_genoflu: settingsDraft.run_genoflu,
        genoflu_db: settingsDraft.genoflu_db,
        genoflu_pident: settingsDraft.genoflu_pident,
        projects_root: settingsDraft.projects_root,
      }),
    })
      .then((r) => r.json())
      .then(() => {
        setIrmaModule(settingsDraft.irma_module || "FLU");
        setRunGenoflu(settingsDraft.run_genoflu !== undefined ? settingsDraft.run_genoflu : true);
        setGenoFluPident(settingsDraft.genoflu_pident || 98);
        setGenoFluDb(settingsDraft.genoflu_db || "");
        loadProjects();
      })
      .catch(() => {});
  }

  // ---------------------------------------------------------------------------
  // Metadata helpers
  // ---------------------------------------------------------------------------
  function loadMetadata(name) {
    setMetadataByProj((m) => ({ ...m, [name]: { ...(m[name] || {}), loading: true } }));
    fetch(`./api/projects/${encodeURIComponent(name)}/metadata`)
      .then((r) => r.json())
      .then((data) => setMetadataByProj((m) => ({ ...m, [name]: { loading: false, ...data } })))
      .catch(() => setMetadataByProj((m) => ({ ...m, [name]: { loading: false, fields: [], samples: [], records: {}, xlsx_present: false } })));
  }

  async function saveMetadata(name) {
    const meta = metadataByProj[name];
    if (!meta) return;
    setMetaSaving((m) => ({ ...m, [name]: true }));
    try {
      const res = await fetch(`./api/projects/${encodeURIComponent(name)}/metadata`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ records: meta.records || {} }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        setMetaImportStatus((m) => ({ ...m, [name]: `Save failed: ${data.detail || res.status}` }));
        return;
      }
      setMetaImportStatus((m) => ({ ...m, [name]: `Saved ${data.count} record(s).` }));
      loadMetadata(name);
    } catch (e) {
      setMetaImportStatus((m) => ({ ...m, [name]: `Save failed: ${e.message}` }));
    } finally {
      setMetaSaving((m) => ({ ...m, [name]: false }));
    }
  }

  function updateMetaCell(name, sample, field, value) {
    setMetadataByProj((m) => {
      const prev = m[name] || {};
      const records = { ...(prev.records || {}) };
      records[sample] = { ...(records[sample] || { sample }), [field]: value };
      return { ...m, [name]: { ...prev, records } };
    });
  }

  function pickMetaXlsx(name) {
    metaUploadProjRef.current = name;
    metaUploadRef.current?.click();
  }

  async function uploadMetaXlsx(name, file) {
    if (!file) return;
    const fd = new FormData();
    fd.append("file", file);
    setMetaImportStatus((m) => ({ ...m, [name]: "Importing Excel…" }));
    try {
      const res = await fetch(`./api/projects/${encodeURIComponent(name)}/metadata/upload`, {
        method: "POST",
        body: fd,
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        setMetaImportStatus((m) => ({ ...m, [name]: `Import failed: ${data.detail || res.status}` }));
        return;
      }
      setMetaImportStatus((m) => ({ ...m, [name]: `Imported ${data.imported} record(s) from Excel.` }));
      setMetadataByProj((m) => ({
        ...m,
        [name]: { ...(m[name] || {}), records: data.records || {}, xlsx_present: true },
      }));
    } catch (e) {
      setMetaImportStatus((m) => ({ ...m, [name]: `Import failed: ${e.message}` }));
    }
  }

  const logLineClass = (line) => {
    if (line.startsWith("$ ")) return "log-line cmd";
    if (line.startsWith("ERROR") || line.startsWith("error")) return "log-line error";
    if (line === "[DONE]") return "log-line done";
    return "log-line";
  };

  const statusText = { idle: "idle", running: "running", succeeded: "succeeded", failed: "failed" }[jobStatus];

  // Results pane data for the selected sample.
  const resTable = selectedResultKey ? irmaTables[selectedResultKey] : null;
  const resFiles = selectedResultKey ? sampleResults[selectedResultKey] : null;
  const assembly = resTable?.assembly || {};
  const genoflu = resTable?.genoflu || {};
  const haCleavage = resTable?.ha_cleavage || {};

  // Active project metadata
  const activeMeta = activeProject ? metadataByProj[activeProject] : null;

  return (
    <div className="app">
      <input
        ref={uploadInputRef}
        type="file"
        multiple
        accept=".fastq.gz,application/gzip"
        style={{ display: "none" }}
        onChange={(e) => {
          const files = Array.from(e.target.files);
          e.target.value = "";
          if (uploadProjRef.current) uploadFiles(uploadProjRef.current, files);
        }}
      />
      <input
        ref={metaUploadRef}
        type="file"
        accept=".xlsx"
        style={{ display: "none" }}
        onChange={(e) => {
          const file = e.target.files[0];
          e.target.value = "";
          if (metaUploadProjRef.current && file) uploadMetaXlsx(metaUploadProjRef.current, file);
        }}
      />
      {/* ── Header ─────────────────────────────────────────────── */}
      <header className="app-header">
        <div className="app-brand">
          <img className="app-logo" src="./irma_icon.svg" alt="IRMA logo" />
          <div>
            <h1>
              IRMA <span className="version-tag">v{APP_VERSION}</span>
            </h1>
            <p>Influenza &amp; SARS-CoV-2 genome assembly via CDC IRMA &mdash; with GenoFLU genotyping and submission-header generation</p>
          </div>
        </div>
        <div className="status-pill">
          <span className="dot" data-state={jobStatus} />
          <span>{statusText}</span>
        </div>
      </header>

      <main className="layout">
        {/* ── Status strip ─────────────────────────────────────── */}
        <section className="status-strip">
          <div className="status-item">
            <span className="status-label">Selected</span>
            <span className="status-value">
              {Object.keys(checkedKeys).length
                ? `${Object.keys(checkedKeys).length} sample${Object.keys(checkedKeys).length > 1 ? "s" : ""}`
                : "—"}
            </span>
          </div>
          <div className="status-item">
            <span className="status-label">Subtype</span>
            <span className="status-value">{assembly.subtype || (resTable?.present ? "—" : "—")}</span>
          </div>
          <div className="status-item">
            <span className="status-label">GenoFLU</span>
            <span className="status-value">{genoflu.genotype || "—"}</span>
          </div>
          <div className="status-item">
            <span className="status-label">Module</span>
            <span className="status-value cap">{irmaModule}</span>
          </div>
          <div className="status-item">
            <span className="status-label">Job</span>
            <span className="status-value cap">
              {jobStatus === "running" ? <><span className="pulse-dot" />running</> : statusText}
            </span>
          </div>
        </section>

        {/* ════════════════════════════════════════════════════════ */}
        {/* SECTION: Settings                                        */}
        {/* ════════════════════════════════════════════════════════ */}
        <div className="row-header">
          <h2>Settings</h2>
          <button className="ghost" onClick={() => {
            if (!showSettings) {
              fetch("./api/config").then((r) => r.json()).then(setSettingsDraft).catch(() => {});
            }
            setShowSettings(!showSettings);
          }}>
            {showSettings ? "Hide" : "Show"}
          </button>
        </div>
        {showSettings && (
          <div className="row-grid row-grid-single">
            <section className="panel">
              <div className="form-section">
                <label className="form-label">IRMA module</label>
                <select
                  value={settingsDraft.irma_module || "FLU"}
                  onChange={(e) => setSettingsDraft((d) => ({ ...d, irma_module: e.target.value }))}
                >
                  {IRMA_MODULES.map((m) => (
                    <option key={m} value={m}>{m}</option>
                  ))}
                </select>
                <div className="form-hint">FLU for influenza A/B/C; CoV for SARS-CoV-2 and other coronaviruses.</div>
              </div>
              <div className="form-section">
                <label className="checkbox-label" style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer" }}>
                  <input
                    type="checkbox"
                    checked={settingsDraft.run_genoflu !== undefined ? settingsDraft.run_genoflu : true}
                    onChange={(e) => setSettingsDraft((d) => ({ ...d, run_genoflu: e.target.checked }))}
                  />
                  <span>Run GenoFLU genotyping (FLU module only)</span>
                </label>
              </div>
              <div className="form-section">
                <label className="form-label">GenoFLU %identity threshold (default 98)</label>
                <input
                  type="number" min="80" max="100" step="0.5"
                  value={settingsDraft.genoflu_pident !== undefined ? settingsDraft.genoflu_pident : 98}
                  onChange={(e) => setSettingsDraft((d) => ({ ...d, genoflu_pident: parseFloat(e.target.value) || 98 }))}
                />
              </div>
              <div className="form-section">
                <label className="form-label">GenoFLU database path (optional)</label>
                <input
                  placeholder="(leave blank to use the default bundled DB)"
                  value={settingsDraft.genoflu_db || ""}
                  onChange={(e) => setSettingsDraft((d) => ({ ...d, genoflu_db: e.target.value }))}
                />
                <div className="form-hint">Leave blank unless you have a custom GenoFLU reference database at a non-default path.</div>
              </div>
              <div className="form-section">
                <label className="form-label">Personal projects root</label>
                <div style={{ display: "flex", gap: 6 }}>
                  <input
                    style={{ flex: 1 }}
                    value={settingsDraft.projects_root || ""}
                    onChange={(e) => setSettingsDraft((d) => ({ ...d, projects_root: e.target.value }))}
                  />
                  <button type="button" className="ghost" onClick={openFolderBrowser}>Browse…</button>
                </div>
                {Array.isArray(settingsDraft.recent_projects_roots) && settingsDraft.recent_projects_roots.length > 0 && (
                  <select
                    style={{ marginTop: 6, width: "100%" }}
                    value=""
                    onChange={(e) => { if (e.target.value) setSettingsDraft((d) => ({ ...d, projects_root: e.target.value })); }}
                  >
                    <option value="">↻ Recent roots…</option>
                    {settingsDraft.recent_projects_roots.map((r) => (
                      <option key={r} value={r}>{r}</option>
                    ))}
                  </select>
                )}
                <div className="form-hint">New projects are created under this root. Shared projects at /srv/kapurlab/projects/ are always visible. Click Save to apply.</div>
              </div>
              <div style={{ display: "flex", justifyContent: "flex-end", gap: 8 }}>
                <button onClick={saveSettings}>Save</button>
              </div>
            </section>
          </div>
        )}

        {/* ════════════════════════════════════════════════════════ */}
        {/* SECTION: Projects & Samples                              */}
        {/* ════════════════════════════════════════════════════════ */}
        <div className="row-header">
          <h2>Projects &amp; Samples</h2>
          <button className="ghost" onClick={() => setShowProjects(!showProjects)}>
            {showProjects ? "Hide" : "Show"}
          </button>
        </div>
        {showProjects && (
          <div className="row-grid row-grid-split">
            {/* LEFT — project / sample browser */}
            <section className="panel">
              <div className="panel-header">
                <h2>Projects</h2>
                <div className="panel-actions">
                  <button className="ghost action" onClick={loadProjects}>↻ Refresh</button>
                </div>
              </div>
              <div className="row">
                <input
                  placeholder="New project name (e.g. Flu_surveillance_2025)"
                  value={newProjectName}
                  onChange={(e) => setNewProjectName(e.target.value.replace(/\s+/g, "_"))}
                  onKeyDown={(e) => { if (e.key === "Enter") createProject(); }}
                  disabled={creatingProject}
                  title="Spaces become underscores. Letters, digits, _ - . are allowed. Created under your personal projects and shared with the sibling GUIs."
                />
                <button onClick={createProject} disabled={creatingProject || !newProjectName.trim()}>
                  {creatingProject ? "Creating…" : "Create"}
                </button>
              </div>
              <div className="form-hint" style={{ marginTop: -4, marginBottom: 8 }}>
                Created under your personal projects root — also visible in vSNP and other GUIs. Add FASTQs to the project's <code>download/</code> folder.
              </div>
              <div className="list project-list">
                {projectsLoading && <div className="loading-text">Loading projects…</div>}
                {!projectsLoading && projects.length === 0 && (
                  <div className="note">No projects found. Check Settings for the projects path.</div>
                )}
                {projects.map((proj) => (
                  <div
                    key={proj.name}
                    className={`list-item ${activeRun?.project === proj.name || activeProject === proj.name ? "active" : ""}`}
                  >
                    <div className="item-top" onClick={() => toggleProject(proj.name)}>
                      <span className="expand-icon">{expanded[proj.name] ? "▾" : "▸"}</span>
                      <div className="list-title" title={proj.name}>{proj.name}</div>
                      <span className={`scope-badge scope-${proj.scope}`}>{proj.scope}</span>
                    </div>
                    {proj.path && <div className="list-path" title={proj.path}>{proj.path}</div>}
                    <div className="list-meta">
                      {proj.fastq_count} FASTQ
                      {proj.irma_runs?.length > 0 &&
                        ` · ${proj.irma_runs.length} IRMA run${proj.irma_runs.length > 1 ? "s" : ""}`}
                    </div>
                    {expanded[proj.name] && (
                      <div className="sample-list">
                        {!samples[proj.name] && <div className="loading-text">Loading samples…</div>}
                        {samples[proj.name]?.length === 0 && (
                          <div className="empty-msg" style={{ paddingLeft: 4 }}>
                            No FASTQ files yet — add some from the <strong>Inputs</strong> pane on the right.
                          </div>
                        )}
                        {samples[proj.name]?.map((s) => {
                          const key = sampleKey(proj.name, s);
                          const res = sampleResults[key];
                          const hasRun = proj.irma_runs?.includes(s.sample);
                          const status = res?.status || (hasRun ? "done" : "none");
                          const checked = !!checkedKeys[key];
                          const open = !!openResults[key];
                          const statusLabel =
                            status === "running" ? "● running" : status === "done" ? "✓ results" : "not run";
                          return (
                          <div
                            key={s.r1}
                            className={`sample-item ${isActive(proj.name, s) ? "active" : ""}`}
                          >
                            <div className="sample-name-row" style={{ display: "flex", alignItems: "center", gap: 8 }}>
                              <input
                                type="checkbox"
                                checked={checked}
                                onChange={() => toggleChecked(proj.name, s)}
                                title="Select for batch run"
                              />
                              <div
                                className="sample-name"
                                title={`${s.sample} — click to show results`}
                                style={{ flex: 1, cursor: "pointer" }}
                                onClick={() => toggleResults(proj.name, s)}
                              >
                                {s.sample}
                              </div>
                              <span className={`read-badge ${s.paired ? "badge-pe" : "badge-se"}`}>
                                {s.paired ? "PE" : "SE"}
                              </span>
                              <span
                                className={`run-status run-status-${status}`}
                                title={`Run status: ${status}`}
                                style={{ fontSize: 11, whiteSpace: "nowrap" }}
                              >
                                {statusLabel}
                              </span>
                              <button
                                className="ghost"
                                style={{ fontSize: 11 }}
                                onClick={() => toggleResults(proj.name, s)}
                                title="Show/hide results for this sample"
                              >
                                {open ? "▾" : "▸"}
                              </button>
                            </div>
                            <div className="sample-files">
                              {s.paired ? (
                                <>
                                  <div className="sample-file-row">
                                    <span className="file-label">R1</span>
                                    <span className="file-name" title={s.r1_name}>{s.r1_name}</span>
                                    <span className="file-size">{fmtSize(s.r1_size)}</span>
                                  </div>
                                  <div className="sample-file-row">
                                    <span className="file-label">R2</span>
                                    <span className="file-name" title={s.r2_name}>{s.r2_name}</span>
                                    <span className="file-size">{fmtSize(s.r2_size)}</span>
                                  </div>
                                </>
                              ) : (
                                <div className="sample-file-row">
                                  <span className="file-name" title={s.r1_name}>{s.r1_name}</span>
                                  <span className="file-size">{fmtSize(s.r1_size)}</span>
                                </div>
                              )}
                            </div>
                            {open && (
                              <div className="sample-results-inline" style={{ marginTop: 6, paddingLeft: 22 }}>
                                <div style={{ display: "flex", gap: 8, marginBottom: 4 }}>
                                  <button
                                    className="ghost action"
                                    disabled={running}
                                    onClick={() => runSamples([{ project: proj.name, ...s }])}
                                  >
                                    {status === "done" ? "↻ Re-run IRMA" : "▶ Run IRMA"}
                                  </button>
                                  <button className="ghost action" onClick={() => { loadSampleResults(proj.name, s); loadIrmaTable(proj.name, s); }}>
                                    ↻ Refresh
                                  </button>
                                  <button className="ghost action" onClick={() => { setSelectedResultKey(key); setShowResults(true); }}>
                                    View results ↓
                                  </button>
                                </div>
                                {res?.loading ? (
                                  <div className="loading-text">Loading results…</div>
                                ) : !res || !res.present || (res.files || []).length === 0 ? (
                                  <div className="empty-msg" style={{ paddingLeft: 0 }}>
                                    {status === "running"
                                      ? "Running… results will appear here when finished."
                                      : "No IRMA results yet for this sample."}
                                  </div>
                                ) : (
                                  <div className="results-list">
                                    {res.files.map((f) => {
                                      const base = `./api/projects/${encodeURIComponent(proj.name)}/file?path=${encodeURIComponent(f.path)}`;
                                      return (
                                        <div key={f.name} className="results-item">
                                          <span className="result-icon">{fileIcon(f.name)}</span>
                                          {f.openable ? (
                                            <a className="result-name result-link" href={`${base}&inline=1`}
                                               target="_blank" rel="noopener noreferrer" title={`Open ${f.name}`}>
                                              {f.label || f.name}
                                            </a>
                                          ) : (
                                            <a className="result-name result-link" href={`${base}&inline=0`}
                                               title={`Download ${f.name}`}>
                                              {f.label || f.name}
                                            </a>
                                          )}
                                          <span className="result-size">{fmtSize(f.size)}</span>
                                          <a className="result-download" href={`${base}&inline=0`} title={`Download ${f.name}`}>⬇</a>
                                        </div>
                                      );
                                    })}
                                  </div>
                                )}
                              </div>
                            )}
                          </div>
                          );
                        })}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            </section>

            {/* RIGHT — Inputs + batch selection */}
            <div style={{ display: "flex", flexDirection: "column", gap: 20, minWidth: 0 }}>
              <section className="panel">
                <div className="panel-header">
                  <h2>Inputs</h2>
                  {projects.length > 0 && (
                    <select
                      value={activeProject}
                      onChange={(e) => selectProject(e.target.value)}
                      title="Project to add FASTQ files to"
                      style={{ width: "auto", maxWidth: "60%", padding: "6px 10px" }}
                    >
                      {projects.map((p) => (
                        <option key={p.name} value={p.name}>{p.name}</option>
                      ))}
                    </select>
                  )}
                </div>
                {!activeProject ? (
                  <div className="empty-msg">
                    Create a project first (top of the Projects panel), then import, upload, or download FASTQ files into it.
                  </div>
                ) : (
                  <div className="input-columns">
                    <div className="input-column">
                      <h3>Bring Your Own Reads</h3>
                      <div className="row" style={{ margin: 0 }}>
                        <input
                          placeholder="/srv/kapurlab/… folder or .fastq.gz file"
                          value={addPath[activeProject] || ""}
                          onChange={(e) => setAddPath((m) => ({ ...m, [activeProject]: e.target.value }))}
                          onKeyDown={(e) => { if (e.key === "Enter") linkLocal(activeProject); }}
                        />
                        <button className="ghost action" onClick={() => linkLocal(activeProject)} disabled={!(addPath[activeProject] || "").trim()}>Link</button>
                      </div>
                      <div className="form-hint">Symlinks every .fastq.gz found — no copying.</div>

                      <div className="block">
                        <h3>Upload / Drag &amp; Drop</h3>
                        <div
                          className="dropzone"
                          onDragOver={(e) => e.preventDefault()}
                          onDrop={(e) => { e.preventDefault(); uploadFiles(activeProject, e.dataTransfer.files); }}
                        >
                          <button type="button" onClick={() => pickFiles(activeProject)}>Choose Files</button>
                          <span className="drop-hint">Or drop FASTQ.GZ files here</span>
                        </div>
                        {addStatus[activeProject] && <div className="note" style={{ marginBottom: 0 }}>{addStatus[activeProject]}</div>}
                      </div>

                      {inputsByProj[activeProject]?.files?.length > 0 && (
                        <div className="block">
                          <h3 style={{ display: "flex", alignItems: "center", gap: 8 }}>
                            <span style={{ flex: 1 }}>
                              Files in download/
                              <span className="muted" style={{ marginLeft: 6, fontWeight: 400, fontSize: 12 }}>
                                ({inputsByProj[activeProject].count}, {fmtSize(inputsByProj[activeProject].total_bytes)})
                              </span>
                            </span>
                            <button className="ghost" style={{ fontSize: 11, padding: "2px 8px" }} onClick={() => loadInputs(activeProject)} title="Refresh">Refresh</button>
                          </h3>
                          <div className="input-files">
                            {inputsByProj[activeProject].files.map((f) => (
                              <div key={f.name} className="input-file-row">
                                <span className="file-name" title={f.name} style={{ flex: 1 }}>{f.name}</span>
                                <span className="file-size">{fmtSize(f.size)}</span>
                                <button className="ghost" style={{ fontSize: 11, padding: "2px 7px" }} title="Remove from download/" onClick={() => deleteInput(activeProject, f.name)}>✕</button>
                              </div>
                            ))}
                          </div>
                        </div>
                      )}
                    </div>

                    <div className="input-column">
                      <h3>SRA Download</h3>
                      <textarea
                        rows={6}
                        placeholder={"SRR/ERR/DRR or SRX/SRS/PRJNA accessions\n(one per line)"}
                        value={sraText[activeProject] || ""}
                        onChange={(e) => setSraText((m) => ({ ...m, [activeProject]: e.target.value }))}
                        style={{ resize: "vertical", fontFamily: "inherit" }}
                      />
                      <button
                        style={{ width: "100%" }}
                        onClick={() => sraDownload(activeProject)}
                        disabled={!parseAccessions(sraText[activeProject]).length || running}
                      >
                        Download{parseAccessions(sraText[activeProject]).length ? ` (${parseAccessions(sraText[activeProject]).length})` : ""}
                      </button>
                      <div className="form-hint">Runs in the background; progress appears in the Pipeline Log.</div>
                    </div>
                  </div>
                )}
              </section>

              <section className="panel">
                <div className="panel-header">
                  <h2>Selected for run</h2>
                  {Object.keys(checkedKeys).length > 0 && (
                    <button className="ghost action" onClick={() => setCheckedKeys({})}>Clear</button>
                  )}
                </div>
                {Object.keys(checkedKeys).length === 0 ? (
                  <div className="empty-msg">
                    Check one or more samples on the left, then run them as a batch from "Run IRMA" below.
                    Click a sample's name to view its results.
                  </div>
                ) : (
                  <div className="selection-box">
                    <div className="sel-title">{Object.keys(checkedKeys).length} sample(s) queued</div>
                    {Object.entries(checkedKeys).map(([key, samp]) => (
                      <div key={key} className="sel-row" style={{ display: "flex", alignItems: "center", gap: 8 }}>
                        <span className="sel-name" style={{ flex: 1 }}>{samp.sample}</span>
                        <span className="muted" style={{ fontSize: 11 }}>{samp.project}</span>
                        <button className="ghost" style={{ fontSize: 11 }}
                                onClick={() => toggleChecked(samp.project, samp)} title="Remove from batch">✕</button>
                      </div>
                    ))}
                  </div>
                )}
              </section>
            </div>
          </div>
        )}

        {/* ════════════════════════════════════════════════════════ */}
        {/* SECTION: Sample Metadata                                 */}
        {/* ════════════════════════════════════════════════════════ */}
        <div className="row-header">
          <h2>Sample Metadata</h2>
          <button className="ghost" onClick={() => {
            if (!showMetadata && activeProject && !metadataByProj[activeProject]) {
              loadMetadata(activeProject);
            }
            setShowMetadata(!showMetadata);
          }}>
            {showMetadata ? "Hide" : "Show"}
          </button>
        </div>
        {showMetadata && (
          <div className="row-grid row-grid-single">
            <section className="panel">
              <div className="panel-header">
                <h2>
                  Submission Metadata
                  {activeProject && <span className="muted" style={{ fontSize: 13, fontWeight: 400, marginLeft: 8 }}>{activeProject}</span>}
                </h2>
                <div className="panel-actions">
                  {projects.length > 0 && (
                    <select
                      value={activeProject}
                      onChange={(e) => {
                        selectProject(e.target.value);
                        if (!metadataByProj[e.target.value]) loadMetadata(e.target.value);
                      }}
                      style={{ width: "auto", maxWidth: 200, padding: "6px 10px" }}
                    >
                      {projects.map((p) => (
                        <option key={p.name} value={p.name}>{p.name}</option>
                      ))}
                    </select>
                  )}
                  {activeProject && (
                    <button className="ghost action" onClick={() => loadMetadata(activeProject)}>↻ Refresh</button>
                  )}
                </div>
              </div>
              <div className="form-hint" style={{ marginBottom: 10 }}>
                Per-sample fields used to build influenza submission FASTA headers. The <strong>sample</strong> column is the match key (read-only). Edit cells inline and click Save, or manage via Excel below.
              </div>

              {!activeProject ? (
                <div className="empty-msg">Select a project to manage its sample metadata.</div>
              ) : !activeMeta ? (
                <div style={{ display: "flex", gap: 8 }}>
                  <button className="ghost action" onClick={() => loadMetadata(activeProject)}>Load metadata for {activeProject}</button>
                </div>
              ) : activeMeta.loading ? (
                <div className="loading-text">Loading metadata…</div>
              ) : (
                <>
                  {/* Editable table */}
                  {activeMeta.fields && activeMeta.fields.length > 0 ? (
                    <div style={{ overflowX: "auto", marginBottom: 12 }}>
                      <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
                        <thead>
                          <tr style={{ textAlign: "left", borderBottom: "2px solid var(--border, #ddd)", background: "var(--panel-2)" }}>
                            {activeMeta.fields.map((f) => (
                              <th key={f} style={{ padding: "6px 8px", whiteSpace: "nowrap" }}>{f}</th>
                            ))}
                          </tr>
                        </thead>
                        <tbody>
                          {Object.keys(activeMeta.records || {}).length === 0 ? (
                            <tr>
                              <td colSpan={(activeMeta.fields || []).length} style={{ padding: "10px 8px", color: "var(--muted)", fontStyle: "italic" }}>
                                No records yet. Add FASTQ files to the project and refresh, or upload an Excel file below.
                              </td>
                            </tr>
                          ) : (
                            Object.values(activeMeta.records || {}).map((row) => (
                              <tr key={row.sample} style={{ borderBottom: "1px solid var(--border, #eee)" }}>
                                {activeMeta.fields.map((f) => (
                                  <td key={f} style={{ padding: "4px 6px" }}>
                                    {f === "sample" ? (
                                      <span style={{ fontWeight: 600, fontSize: 12 }}>{row.sample}</span>
                                    ) : (
                                      <input
                                        style={{ padding: "3px 6px", fontSize: 12, borderRadius: 6, width: "100%", minWidth: 80 }}
                                        value={row[f] || ""}
                                        onChange={(e) => updateMetaCell(activeProject, row.sample, f, e.target.value)}
                                        title={`${f} for ${row.sample}`}
                                      />
                                    )}
                                  </td>
                                ))}
                              </tr>
                            ))
                          )}
                        </tbody>
                      </table>
                    </div>
                  ) : (
                    <div className="note">No metadata fields available.</div>
                  )}

                  {/* Actions row */}
                  <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
                    <button
                      onClick={() => saveMetadata(activeProject)}
                      disabled={metaSaving[activeProject]}
                    >
                      {metaSaving[activeProject] ? "Saving…" : "Save metadata"}
                    </button>
                    <a
                      href={`./api/projects/${encodeURIComponent(activeProject)}/metadata.xlsx`}
                      className="ghost"
                      style={{ textDecoration: "none", padding: "6px 12px", borderRadius: 10, fontSize: 13, border: "1px solid var(--border)", display: "inline-block" }}
                      title="Download as Excel workbook"
                    >
                      Download Excel
                      {activeMeta.xlsx_present && <span className="muted" style={{ marginLeft: 6, fontSize: 11 }}>(exists)</span>}
                    </a>
                    <button className="ghost" onClick={() => pickMetaXlsx(activeProject)} title="Replace metadata from an Excel file">
                      Replace from Excel…
                    </button>
                    {metaImportStatus[activeProject] && (
                      <span className="note" style={{ marginTop: 0 }}>{metaImportStatus[activeProject]}</span>
                    )}
                  </div>
                  <div className="form-hint" style={{ marginTop: 6 }}>
                    Replace from Excel: the uploaded sheet must have a <code>sample</code> (or accession) column to match rows.
                    {activeMeta.xlsx_present && " A mirror Excel is already saved for this project."}
                  </div>
                </>
              )}
            </section>
          </div>
        )}

        {/* ════════════════════════════════════════════════════════ */}
        {/* SECTION: Run IRMA                                        */}
        {/* ════════════════════════════════════════════════════════ */}
        <div className="row-header">
          <h2>Run IRMA</h2>
          <button className="ghost" onClick={() => setShowRun(!showRun)}>
            {showRun ? "Hide" : "Show"}
          </button>
        </div>
        {showRun && (
          <div className="row-grid row-grid-split">
            {/* LEFT — configure & run */}
            <section className="panel">
              <h2>Configure &amp; Run</h2>

              <div className="form-section">
                <label className="form-label">IRMA module</label>
                <select
                  value={irmaModule}
                  onChange={(e) => setIrmaModule(e.target.value)}
                  disabled={running}
                >
                  {IRMA_MODULES.map((m) => (
                    <option key={m} value={m}>{m}</option>
                  ))}
                </select>
                <div className="note" style={{ marginTop: 4 }}>
                  FLU for influenza A/B/C; CoV for SARS-CoV-2 and other coronaviruses.
                </div>
              </div>

              <div className="form-section">
                <label className="form-label">Submission header style</label>
                <select
                  value={headerStyle}
                  onChange={(e) => setHeaderStyle(e.target.value)}
                  disabled={running}
                >
                  {HEADER_STYLES.map((h) => (
                    <option key={h} value={h}>{h}</option>
                  ))}
                </select>
                <div className="note" style={{ marginTop: 4 }}>
                  <code>ncbi</code>: generates NCBI-format FASTA headers from metadata. <code>strain</code>: uses strain name only.
                </div>
              </div>

              <div className="form-section">
                <label className="checkbox-label" style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer" }}>
                  <input type="checkbox" checked={runGenoflu} onChange={(e) => setRunGenoflu(e.target.checked)} disabled={running} />
                  <span>Run GenoFLU genotyping (FLU module only)</span>
                </label>
                {runGenoflu && (
                  <div style={{ marginTop: 8, paddingLeft: 22 }}>
                    <label className="form-label">GenoFLU %identity</label>
                    <input
                      type="number" min="80" max="100" step="0.5"
                      value={genoFluPident}
                      onChange={(e) => setGenoFluPident(parseFloat(e.target.value) || 98)}
                      disabled={running}
                      style={{ width: 100 }}
                    />
                    <label className="form-label" style={{ marginTop: 8 }}>GenoFLU DB path (optional override)</label>
                    <input
                      placeholder="(use default DB)"
                      value={genoFluDb}
                      onChange={(e) => setGenoFluDb(e.target.value)}
                      disabled={running}
                    />
                  </div>
                )}
              </div>

              <button
                className="run-btn"
                onClick={runSelected}
                disabled={running || Object.keys(checkedKeys).length === 0}
              >
                {running
                  ? `Running… ${queueInfo.total > 1 ? `(${queueInfo.done}/${queueInfo.total})` : ""}`
                  : `▶ Run selected${Object.keys(checkedKeys).length ? ` (${Object.keys(checkedKeys).length})` : ""}`}
              </button>
              {Object.keys(checkedKeys).length === 0 && (
                <div className="note">Check one or more samples on the left to enable the run. (Or use "Run IRMA" under any sample.)</div>
              )}
            </section>

            {/* RIGHT — current run status */}
            <section className="panel">
              <div className="panel-header">
                <h2>Current run</h2>
                {jobId && <span className="muted" style={{ fontSize: 12 }}>job {jobId.slice(0, 8)}</span>}
              </div>
              {activeRun ? (
                <div className="selection-box">
                  <div className="sel-title">
                    {jobStatus === "running" ? "Running" : jobStatus === "succeeded" ? "Done" : jobStatus}
                    {queueInfo.total > 1 ? ` — ${queueInfo.done}/${queueInfo.total} in batch` : ""}
                  </div>
                  <div><span className="sel-name">{activeRun.sample}</span></div>
                  <div style={{ marginTop: 2 }}>
                    <span className="muted">Project:</span> <strong>{activeRun.project}</strong>
                  </div>
                  {currentStep && <div className="muted" style={{ marginTop: 4 }}>{currentStep}</div>}
                  <div className="note" style={{ marginTop: 8 }}>
                    Files appear inline under each sample on the left; the assembly summary is in the Results section below.
                  </div>
                </div>
              ) : (
                <div className="empty-msg">
                  No active run. Select samples, set options, and Run. Results for any sample are shown below.
                </div>
              )}
            </section>
          </div>
        )}

        {/* ════════════════════════════════════════════════════════ */}
        {/* SECTION: Results                                         */}
        {/* ════════════════════════════════════════════════════════ */}
        <div className="row-header">
          <h2>Results</h2>
          <button className="ghost" onClick={() => setShowResults(!showResults)}>
            {showResults ? "Hide" : "Show"}
          </button>
        </div>
        {showResults && (
          <div className="row-grid row-grid-single">
            <section className="panel">
              {!resTable ? (
                <div className="empty-msg">
                  Click a sample's name in the Projects tree to load its IRMA results here.
                </div>
              ) : resTable.loading ? (
                <div className="loading-text">Loading results…</div>
              ) : !resTable.present ? (
                <div className="empty-msg">No IRMA results for {selectedResultKey?.split("::")[1]} yet — run it first.</div>
              ) : (
                <>
                  <div className="panel-header">
                    <h2>{selectedResultKey?.split("::")[1]}</h2>
                    <div className="panel-actions" style={{ display: "flex", gap: 10, alignItems: "center" }}>
                      {assembly.subtype && (
                        <span className="muted" style={{ fontSize: 12 }}>
                          subtype: <strong>{assembly.subtype}</strong>
                        </span>
                      )}
                      {genoflu.genotype && (
                        <span className="muted" style={{ fontSize: 12 }}>
                          GenoFLU: <strong>{genoflu.genotype}</strong>
                        </span>
                      )}
                      {assembly.overall_verdict && (
                        <span
                          style={{
                            fontSize: 12, fontWeight: 700, padding: "2px 8px", borderRadius: 999,
                            background: assembly.overall_verdict === "PASS" ? "#6baa75" :
                                        assembly.overall_verdict === "FAIL" ? "#c46a6a" : "#d8b26e",
                            color: "#fff",
                          }}
                        >
                          {assembly.overall_verdict}
                        </span>
                      )}
                    </div>
                  </div>

                  {/* HA cleavage site */}
                  {haCleavage.motif && (
                    <div className="note" style={{ marginBottom: 8 }}>
                      <strong>HA cleavage:</strong> <code>{haCleavage.motif}</code>
                      {haCleavage.multibasic !== undefined && (
                        <span style={{ marginLeft: 8, color: haCleavage.multibasic ? "#c46a6a" : "#6baa75", fontWeight: 600 }}>
                          {haCleavage.multibasic ? "multibasic (HPAI)" : "monobasic (LPAI)"}
                        </span>
                      )}
                      {haCleavage.note && <span className="muted" style={{ marginLeft: 6 }}>{haCleavage.note}</span>}
                    </div>
                  )}

                  {/* Segment table */}
                  {Array.isArray(assembly.segments) && assembly.segments.length > 0 && (
                    <div style={{ overflowX: "auto", marginBottom: 12 }}>
                      <table className="result-table" style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
                        <thead>
                          <tr style={{ textAlign: "left", borderBottom: "2px solid var(--border, #ddd)" }}>
                            <th style={{ padding: "6px 8px" }}>Segment</th>
                            <th style={{ padding: "6px 8px" }}>Reference</th>
                            <th style={{ padding: "6px 8px", textAlign: "right" }}>Length (bp)</th>
                            <th style={{ padding: "6px 8px", textAlign: "right" }}>Mean Depth</th>
                            <th style={{ padding: "6px 8px", textAlign: "right" }}>%&lt;10X</th>
                            <th style={{ padding: "6px 8px", textAlign: "right" }}>%Zero</th>
                            <th style={{ padding: "6px 8px" }}>Verdict</th>
                          </tr>
                        </thead>
                        <tbody>
                          {assembly.segments.map((seg, i) => (
                            <tr key={i} style={{ borderBottom: "1px solid var(--border, #eee)" }}>
                              <td style={{ padding: "5px 8px", fontWeight: 600 }}>{seg.gene}</td>
                              <td style={{ padding: "5px 8px", maxWidth: 200, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }} title={seg.reference_name}>{seg.reference_name}</td>
                              <td style={{ padding: "5px 8px", textAlign: "right" }}>{seg.length != null ? Number(seg.length).toLocaleString() : "—"}</td>
                              <td style={{ padding: "5px 8px", textAlign: "right" }}>{seg.avg_depth != null ? Number(seg.avg_depth).toFixed(1) : "—"}</td>
                              <td style={{ padding: "5px 8px", textAlign: "right" }}>{seg.pct_lt10x != null ? `${seg.pct_lt10x}%` : "—"}</td>
                              <td style={{ padding: "5px 8px", textAlign: "right" }}>{seg.pct_zero_cov != null ? `${seg.pct_zero_cov}%` : "—"}</td>
                              <td style={{ padding: "5px 8px" }}>
                                {seg.verdict && (
                                  <span style={{
                                    fontSize: 11, fontWeight: 700, padding: "1px 7px", borderRadius: 999,
                                    background: seg.verdict === "PASS" ? "#6baa75" :
                                                seg.verdict === "FAIL" ? "#c46a6a" : "#d8b26e",
                                    color: "#fff",
                                  }}>
                                    {seg.verdict}
                                  </span>
                                )}
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}

                  {/* Primary deliverables + all result files */}
                  {resFiles?.files?.length > 0 && (
                    <div className="results-list" style={{ marginBottom: 12 }}>
                      {resFiles.files.map((f) => {
                        const base = `./api/projects/${encodeURIComponent(selectedResultKey.split("::")[0])}/file?path=${encodeURIComponent(f.path)}`;
                        const isPrimary = ["report_pdf", "submission_fasta", "stats_xlsx"].includes(f.category);
                        return (
                          <div key={f.name} className="results-item" style={isPrimary ? { border: "1px solid var(--accent)", background: "rgba(76,140,138,0.05)" } : {}}>
                            <span className="result-icon">{fileIcon(f.name)}</span>
                            <a className="result-name result-link" href={`${base}&inline=${f.openable ? 1 : 0}`}
                               target={f.openable ? "_blank" : undefined} rel="noopener noreferrer">
                              {isPrimary ? <strong>{f.label || f.name}</strong> : (f.label || f.name)}
                            </a>
                            <span className="result-size">{fmtSize(f.size)}</span>
                            <a className="result-download" href={`${base}&inline=0`} title={`Download ${f.name}`}>⬇</a>
                          </div>
                        );
                      })}
                    </div>
                  )}

                  {/* Provenance disclosure */}
                  {resTable.provenance && Object.keys(resTable.provenance).length > 0 && (
                    <details style={{ marginTop: 14 }}>
                      <summary style={{ cursor: "pointer", fontWeight: 600 }}>Run provenance</summary>
                      <div className="note" style={{ marginTop: 8 }}>
                        <pre style={{ fontSize: 11, whiteSpace: "pre-wrap", wordBreak: "break-all", margin: 0 }}>
                          {JSON.stringify(resTable.provenance, null, 2)}
                        </pre>
                      </div>
                    </details>
                  )}
                </>
              )}
            </section>
          </div>
        )}

        {/* ════════════════════════════════════════════════════════ */}
        {/* SECTION: Pipeline Log                                    */}
        {/* ════════════════════════════════════════════════════════ */}
        <div className="row-header">
          <h2>Pipeline Log</h2>
          <button className="ghost" onClick={() => setShowLogs(!showLogs)}>
            {showLogs ? "Hide" : "Show"}
          </button>
        </div>
        {showLogs && (
          <div className="row-grid row-grid-single">
            <section className="panel">
              <div className="log-meta">
                <span className="dot" data-state={jobStatus} />
                <span style={{ fontWeight: 600 }}>
                  {jobStatus === "idle" && "Idle"}
                  {jobStatus === "running" && "Running"}
                  {jobStatus === "succeeded" && "Done"}
                  {jobStatus === "failed" && "Failed"}
                </span>
                {jobStatus === "running" && currentStep && (
                  <span className="log-step" title={currentStep}>— {currentStep}</span>
                )}
              </div>
              <div className="log" ref={logRef}>
                {logLines.length === 0 ? (
                  <span className="log-placeholder">
                    {jobStatus === "idle"
                      ? "Select a sample and click Run to start."
                      : "Waiting for output…"}
                  </span>
                ) : (
                  logLines.map((line, i) => (
                    <div key={i} className={logLineClass(line)}>{line}</div>
                  ))
                )}
              </div>
            </section>
          </div>
        )}
      </main>

      {folderBrowser.open && (
        <div
          onClick={() => setFolderBrowser((s) => ({ ...s, open: false }))}
          style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,0.45)", display: "flex", alignItems: "center", justifyContent: "center", zIndex: 1000 }}
        >
          <div
            onClick={(e) => e.stopPropagation()}
            style={{ background: "var(--panel, #fff)", color: "inherit", borderRadius: 10, width: "min(640px, 92vw)", maxHeight: "80vh", display: "flex", flexDirection: "column", boxShadow: "0 10px 40px rgba(0,0,0,0.3)" }}
          >
            <div style={{ padding: "12px 16px", borderBottom: "1px solid var(--border, #ddd)", fontWeight: 700 }}>
              Select a projects root
            </div>
            <div style={{ padding: "10px 16px", display: "flex", gap: 6, alignItems: "center" }}>
              <button type="button" className="ghost" disabled={!folderBrowser.parent || folderBrowser.loading} onClick={() => browseDirs(folderBrowser.parent)}>↑ Up</button>
              <input
                style={{ flex: 1 }}
                value={folderBrowser.path}
                onChange={(e) => setFolderBrowser((s) => ({ ...s, path: e.target.value }))}
                onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); browseDirs(folderBrowser.path); } }}
              />
              <button type="button" className="ghost" onClick={() => browseDirs(folderBrowser.path)}>Go</button>
            </div>
            <div style={{ flex: 1, overflow: "auto", padding: "0 16px", minHeight: 160 }}>
              {folderBrowser.loading ? (
                <div className="note" style={{ padding: 12 }}>Loading…</div>
              ) : folderBrowser.error ? (
                <div className="note" style={{ padding: 12, color: "var(--danger, #c00)" }}>{folderBrowser.error}</div>
              ) : folderBrowser.entries.length === 0 ? (
                <div className="note" style={{ padding: 12 }}>No sub-folders here.</div>
              ) : (
                folderBrowser.entries.map((e) => (
                  <div
                    key={e.path}
                    onClick={() => browseDirs(e.path)}
                    style={{ padding: "7px 8px", cursor: "pointer", borderRadius: 6, display: "flex", gap: 8, alignItems: "center" }}
                    onMouseEnter={(ev) => (ev.currentTarget.style.background = "var(--panel-2, #f0f0f0)")}
                    onMouseLeave={(ev) => (ev.currentTarget.style.background = "transparent")}
                  >
                    <span>📁</span><span>{e.name}</span>
                  </div>
                ))
              )}
            </div>
            <div style={{ padding: "12px 16px", borderTop: "1px solid var(--border, #ddd)", display: "flex", justifyContent: "flex-end", gap: 8 }}>
              <button type="button" className="ghost" onClick={() => setFolderBrowser((s) => ({ ...s, open: false }))}>Cancel</button>
              <button type="button" onClick={chooseFolder} disabled={folderBrowser.loading || !folderBrowser.path}>Select this folder</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
