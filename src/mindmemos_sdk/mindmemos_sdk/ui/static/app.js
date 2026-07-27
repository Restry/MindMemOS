const TAB_COPY = {
  overview: {
    title: "Overview",
    description: "Configure and inspect the SDK from one local workspace.",
  },
  skills: {
    title: "Skills",
    description: "Register, inspect, compare, and update the Skills managed by this SDK.",
  },
  memory: {
    title: "Memory",
    description: "View active memories owned by the configured user through the cloud API.",
  },
  lite: {
    title: "Lite",
    description: "Inspect local Lite runtime traces and SQLite observability records.",
  },
  settings: {
    title: "Settings",
    description: "Configure connection, identity, operation defaults, storage, and network behavior.",
  },
};

let configState = null;
let skillsState = [];
let memoryLoaded = false;
let liteTraceListState = null;
let liteTraceDetailState = null;
let activeLiteTraceKey = null;
let activeLiteSpanId = null;
let liteTraceRequestToken = 0;
const compareContentState = { left: "", right: "" };
const comparePaneMessages = {
  left: "Select a Skill to load its content.",
  right: "Select a Skill to load its content.",
};
const compareRequestTokens = { left: 0, right: 0 };
const compareVersionRequestTokens = { left: 0, right: 0 };
let activeSkillId = null;
let activeSkillPayload = null;
let activeVersionId = null;
let activeEditorSavedContent = "";
const uiLaunchToken = new URLSearchParams(window.location.search).get("token") || "";
if (window.location.search.includes("token=")) {
  history.replaceState(null, "", `${window.location.pathname}${window.location.hash}`);
}

const apiRequest = async (path, options = {}) => {
  const response = await fetch(path, {
    headers: {
      "Content-Type": "application/json",
      ...(uiLaunchToken ? { "X-MindMemOS-UI-Token": uiLaunchToken } : {}),
      ...(options.headers || {}),
    },
    ...options,
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.message || `Request failed (${response.status})`);
  return payload;
};

const setNotice = (message, tone = "info") => {
  const notice = document.querySelector("#settings-notice");
  if (!notice) return;
  notice.dataset.tone = tone;
  notice.querySelector("span:last-child").textContent = message;
};

const setMemoryNotice = (message, tone = "info") => {
  const notice = document.querySelector("#memory-notice");
  if (!notice) return;
  notice.dataset.tone = tone;
  notice.querySelector("span:last-child").textContent = message;
};

const setFieldValue = (id, value) => {
  const field = document.querySelector(`#${id}`);
  if (field) field.value = value ?? "";
};

const renderConfig = (config) => {
  configState = config;
  const defaults = config.defaults || {};
  const memory = config.memory || {};
  const storage = config.storage || {};
  const network = config.network || {};
  const configured = Boolean(config.api_key_configured);

  document.querySelector("#preview-badge").textContent = "SDK API · READY";
  document.querySelector("#connection-badge").textContent = configured ? "CONFIGURED" : "NOT CONFIGURED";
  document.querySelector("#overview-connection-value").textContent = configured ? "Configured" : "Not configured";
  document.querySelector("#overview-connection-detail").textContent = config.base_url || "No API endpoint";
  const identity = defaults.user_id || defaults.app_id || defaults.agent_id || defaults.session_id;
  document.querySelector("#overview-identity-value").textContent = identity ? "Configured" : "No default identity";
  document.querySelector("#overview-identity-detail").textContent = identity ? "Default request identity is available" : "user / app / agent / session";
  document.querySelector("#overview-storage-value").textContent = "Ready";
  document.querySelector("#overview-storage-detail").textContent = config.config_path || "Local config file";
  document.querySelector("#memory-owner").textContent = defaults.user_id
    ? `User: ${defaults.user_id}`
    : "User: not configured";

  setFieldValue("setting-base-url", config.base_url);
  const apiKey = document.querySelector("#setting-api-key");
  apiKey.value = "";
  apiKey.placeholder = configured ? `Configured (${config.api_key_masked}) · enter a new key to replace` : "Not configured";
  setFieldValue("setting-user-id", defaults.user_id);
  setFieldValue("setting-app-id", defaults.app_id);
  setFieldValue("setting-agent-id", defaults.agent_id);
  setFieldValue("setting-session-id", defaults.session_id);
  setFieldValue("setting-search-top-k", memory.search_top_k);
  setFieldValue("setting-search-strategy", memory.search_strategy || "fast");
  setFieldValue("setting-search-rerank", String(Boolean(memory.search_rerank)));
  setFieldValue("setting-search-score-threshold", memory.search_score_threshold);
  setFieldValue("setting-get-top-k", memory.get_top_k);
  setFieldValue("setting-feedback-mode", memory.feedback_mode || "");
  setFieldValue("setting-add-mode", memory.add_mode || "sync");
  setFieldValue("setting-add-role", memory.add_default_role || "user");
  setFieldValue("setting-dreaming-mode", memory.dreaming_mode || "async");
  setFieldValue("setting-search-filters", JSON.stringify(memory.search_filters || {}, null, 2));
  setFieldValue("setting-get-filters", JSON.stringify(memory.get_filters || {}, null, 2));
  document.querySelector("#setting-auto-skill-context").checked = memory.add_auto_skill_context !== false;
  setFieldValue("setting-cache-dir", storage.skill_cache_dir);
  setFieldValue("setting-backup-dir", storage.skill_backup_dir);
  setFieldValue("setting-timeout", network.timeout_seconds);
  setFieldValue("setting-retries", network.max_retries);
  setNotice(`Loaded ${config.config_path}. Changes are saved atomically by the local SDK service.`);
};

const formatMemoryTime = (value) => {
  if (!value) return "Time not available";
  return String(value).replace("T", " ").replace("Z", " UTC");
};

const renderMemories = (payload) => {
  const memories = payload.memories || [];
  const list = document.querySelector("#memory-list");
  document.querySelector("#memory-connection-badge").textContent = "READY";
  document.querySelector("#memory-owner").textContent = payload.user_id
    ? `User: ${payload.user_id}`
    : "User: not configured";
  document.querySelector("#memory-count").textContent = `${memories.length} memor${memories.length === 1 ? "y" : "ies"}`;
  document.querySelector("#overview-memory-value").textContent = `${memories.length} loaded`;
  document.querySelector("#overview-memory-detail").textContent = payload.mode === "search"
    ? "Search results for configured user"
    : "Active memories for configured user";
  if (!memories.length) {
    list.innerHTML = '<div class="memory-empty-state"><div class="empty-icon small">◌</div><strong>No active memories found</strong><span>Try another query or add memory through the cloud API.</span></div>';
    return;
  }
  list.innerHTML = memories.map((memory) => {
    const type = memory.memory_type || "memory";
    const timestamp = memory.last_update_at || memory.created_at || memory.event_time;
    const metadata = [
      memory.event_time ? `Event: ${formatMemoryTime(memory.event_time)}` : null,
      memory.source_timestamp ? `Source: ${formatMemoryTime(memory.source_timestamp)}` : null,
    ].filter(Boolean).join(" · ");
    return `<article class="memory-item">
      <div class="memory-item-top"><span class="memory-kind">${escapeHtml(type)}</span><code class="memory-id">${escapeHtml(memory.id || "unknown-id")}</code><span class="memory-time">${escapeHtml(formatMemoryTime(timestamp))}</span></div>
      <p class="memory-content">${escapeHtml(memory.memory || "(empty memory)")}</p>
      ${metadata ? `<div class="memory-item-meta">${escapeHtml(metadata)}</div>` : ""}
    </article>`;
  }).join("");
};

const memoryRequestPath = (search = false) => {
  const topK = document.querySelector("#memory-top-k").value.trim();
  const query = document.querySelector("#memory-query").value.trim();
  const params = new URLSearchParams();
  if (topK) params.set("top_k", topK);
  if (search && query) params.set("q", query);
  const suffix = params.toString() ? `?${params.toString()}` : "";
  return `${search ? "/api/v1/memories/search" : "/api/v1/memories"}${suffix}`;
};

const loadMemories = async (path = memoryRequestPath(false)) => {
  const list = document.querySelector("#memory-list");
  document.querySelector("#memory-connection-badge").textContent = "LOADING";
  list.innerHTML = '<div class="memory-empty-state"><div class="empty-icon small">◌</div><strong>Loading memories…</strong><span>Fetching the configured user scope from the local SDK service.</span></div>';
  setMemoryNotice("Loading active memories through the local SDK service…");
  try {
    const payload = await apiRequest(path);
    memoryLoaded = true;
    renderMemories(payload);
    setMemoryNotice(`Loaded ${payload.count || 0} memories for the configured user.`, "success");
  } catch (error) {
    memoryLoaded = false;
    document.querySelector("#memory-connection-badge").textContent = "UNAVAILABLE";
    document.querySelector("#overview-memory-value").textContent = "Unavailable";
    document.querySelector("#overview-memory-detail").textContent = error.message;
    document.querySelector("#memory-count").textContent = "Memory unavailable";
    list.innerHTML = `<div class="memory-empty-state"><div class="empty-icon small">!</div><strong>Unable to load Memory</strong><span>${escapeHtml(error.message)}</span></div>`;
    setMemoryNotice(error.message, "error");
  }
};

const setLiteNotice = (message, tone = "info") => {
  const notice = document.querySelector("#lite-notice");
  if (!notice) return;
  notice.dataset.tone = tone;
  notice.querySelector("span:last-child").textContent = message;
};

const formatDuration = (value) => {
  const milliseconds = Number(value || 0);
  if (milliseconds >= 60000) return `${(milliseconds / 60000).toFixed(2)} min`;
  if (milliseconds >= 1000) return `${(milliseconds / 1000).toFixed(2)} s`;
  if (milliseconds >= 1) return `${milliseconds.toFixed(milliseconds >= 100 ? 0 : 2)} ms`;
  return `${(milliseconds * 1000).toFixed(1)} µs`;
};

const formatTraceTime = (value) => {
  if (!value) return "Time unavailable";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return String(value);
  return parsed.toLocaleString(undefined, {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
};

const liteTraceKey = (trace) => `${trace.source}:${trace.trace_id}`;

const renderLiteTraceList = (payload) => {
  liteTraceListState = payload;
  const traces = payload.traces || [];
  const list = document.querySelector("#lite-trace-list");
  document.querySelector("#lite-database-badge").textContent = `${payload.database_count || 0} DB`;
  document.querySelector("#lite-trace-directory-label").textContent = payload.directory || "No directory selected";
  document.querySelector("#lite-trace-count").textContent = `${payload.total || 0} trace${payload.total === 1 ? "" : "s"}`;
  if (!traces.length) {
    list.innerHTML = '<div class="lite-empty-state"><div class="empty-icon small">⌁</div><strong>No traces found</strong><span>The selected Lite databases do not contain any completed traces.</span></div>';
    return;
  }

  list.innerHTML = traces.map((trace) => {
    const key = liteTraceKey(trace);
    const active = key === activeLiteTraceKey ? " active" : "";
    const status = trace.status_code === "ERROR" ? " · ERROR" : "";
    return `<button class="lite-trace-row${active}" type="button" data-trace-key="${escapeHtml(key)}" data-trace-id="${escapeHtml(trace.trace_id)}" data-trace-source="${escapeHtml(trace.source)}">
      <span class="lite-trace-name" title="${escapeHtml(trace.root_span_name)}">${escapeHtml(trace.root_span_name)}</span>
      <span class="lite-trace-duration">${escapeHtml(formatDuration(trace.duration_ms))}</span>
      <span class="lite-trace-id">traceId ${escapeHtml(trace.trace_id)}</span>
      <span class="lite-trace-meta">${escapeHtml(formatTraceTime(trace.start_time))} · ${trace.span_count} spans · ${trace.llm_call_count} LLM${escapeHtml(status)}</span>
    </button>`;
  }).join("");

  list.querySelectorAll("[data-trace-id]").forEach((row) => {
    row.addEventListener("click", () => loadLiteTraceDetail({
      trace_id: row.dataset.traceId,
      source: row.dataset.traceSource,
    }));
  });
};

const loadLiteTraces = async () => {
  const directory = document.querySelector("#lite-trace-directory").value.trim();
  const limit = document.querySelector("#lite-trace-limit").value.trim() || "100";
  const list = document.querySelector("#lite-trace-list");
  if (!directory) {
    setLiteNotice("Enter a Lite trace directory or SQLite database path.", "error");
    return;
  }
  const requestToken = ++liteTraceRequestToken;
  activeLiteTraceKey = null;
  activeLiteSpanId = null;
  liteTraceDetailState = null;
  document.querySelector("#lite-database-badge").textContent = "LOADING";
  list.innerHTML = '<div class="lite-empty-state"><div class="empty-icon small">⌁</div><strong>Scanning Lite traces…</strong><span>Opening matching SQLite databases in read-only mode.</span></div>';
  document.querySelector("#lite-flame-chart").innerHTML = '<div class="lite-empty-state"><div class="empty-icon">◇</div><strong>Waiting for a trace</strong><span>The first root span will open after the directory scan completes.</span></div>';
  document.querySelector("#lite-span-detail").innerHTML = '<div class="lite-empty-state compact"><strong>Span details</strong><span>Select a trace, then click a flame block.</span></div>';
  setLiteNotice("Scanning the selected directory for MindMemOS Lite trace databases…");
  try {
    const params = new URLSearchParams({ directory, limit });
    const payload = await apiRequest(`/api/v1/lite/traces?${params.toString()}`);
    if (requestToken !== liteTraceRequestToken) return;
    renderLiteTraceList(payload);
    setLiteNotice(`Loaded ${payload.count || 0} of ${payload.total || 0} traces from ${payload.database_count || 0} SQLite database${payload.database_count === 1 ? "" : "s"}.`, "success");
    if ((payload.traces || []).length) await loadLiteTraceDetail(payload.traces[0], { requestToken });
  } catch (error) {
    if (requestToken !== liteTraceRequestToken) return;
    liteTraceListState = null;
    document.querySelector("#lite-database-badge").textContent = "UNAVAILABLE";
    document.querySelector("#lite-trace-count").textContent = "Trace data unavailable";
    list.innerHTML = `<div class="lite-empty-state"><div class="empty-icon small">!</div><strong>Unable to load Lite traces</strong><span>${escapeHtml(error.message)}</span></div>`;
    setLiteNotice(error.message, "error");
  }
};

const loadLiteTraceDetail = async (trace, { requestToken = null } = {}) => {
  if (!trace?.trace_id || !trace?.source) return;
  const detailToken = requestToken ?? ++liteTraceRequestToken;
  if (requestToken !== null) liteTraceRequestToken = requestToken;
  activeLiteTraceKey = liteTraceKey(trace);
  activeLiteSpanId = null;
  if (liteTraceListState) renderLiteTraceList(liteTraceListState);
  document.querySelector("#lite-trace-status").textContent = "LOADING";
  document.querySelector("#lite-flame-chart").innerHTML = '<div class="lite-empty-state"><div class="empty-icon">◇</div><strong>Loading trace subtree…</strong><span>Reading spans, events, and LLM projections from SQLite.</span></div>';
  try {
    const directory = document.querySelector("#lite-trace-directory").value.trim();
    const params = new URLSearchParams({ directory, source: trace.source });
    const payload = await apiRequest(`/api/v1/lite/traces/${encodeURIComponent(trace.trace_id)}?${params.toString()}`);
    if (detailToken !== liteTraceRequestToken) return;
    liteTraceDetailState = payload;
    activeLiteSpanId = payload.trace.root_span_id || payload.spans?.[0]?.span_id || null;
    renderLiteFlameChart(payload);
    renderLiteSpanDetail(activeLiteSpanId);
  } catch (error) {
    if (detailToken !== liteTraceRequestToken) return;
    document.querySelector("#lite-trace-status").textContent = "UNAVAILABLE";
    document.querySelector("#lite-flame-chart").innerHTML = `<div class="lite-empty-state"><div class="empty-icon">!</div><strong>Unable to load trace</strong><span>${escapeHtml(error.message)}</span></div>`;
    setLiteNotice(error.message, "error");
  }
};

const buildFlameLayout = (spans) => {
  const sorted = [...spans].sort((left, right) => {
    if (left.depth !== right.depth) return left.depth - right.depth;
    if (left.start_offset_ms !== right.start_offset_ms) return left.start_offset_ms - right.start_offset_ms;
    return right.duration_ms - left.duration_ms;
  });
  const lanesByDepth = new Map();
  const placed = sorted.map((span) => {
    const lanes = lanesByDepth.get(span.depth) || [];
    let lane = lanes.findIndex((end) => end <= span.start_offset_ms + 0.001);
    if (lane < 0) lane = lanes.length;
    lanes[lane] = Math.max(lanes[lane] || 0, span.end_offset_ms);
    lanesByDepth.set(span.depth, lanes);
    return { span, lane };
  });
  const depthOffsets = new Map();
  let rowCount = 0;
  [...lanesByDepth.keys()].sort((left, right) => left - right).forEach((depth) => {
    depthOffsets.set(depth, rowCount);
    rowCount += lanesByDepth.get(depth).length;
  });
  return {
    rows: placed.map(({ span, lane }) => ({ span, row: depthOffsets.get(span.depth) + lane })),
    rowCount,
  };
};

const flameBlockTone = (span) => {
  if (span.status_code === "ERROR") return "error";
  if (span.llm_call || String(span.name).startsWith("llm.")) return "llm";
  if (span.attributes?.["db.system"] || String(span.name).includes("persistence")) return "database";
  return `depth-${Math.min(Number(span.depth || 0), 3)}`;
};

const renderLiteFlameChart = (payload) => {
  const trace = payload.trace || {};
  const spans = payload.spans || [];
  const chart = document.querySelector("#lite-flame-chart");
  const rootSpan = spans.find((span) => span.span_id === trace.root_span_id) || spans[0];
  document.querySelector("#lite-trace-heading h2").textContent = rootSpan?.name || "Trace";
  document.querySelector("#lite-trace-heading .card-description").textContent = `${trace.trace_id} · ${payload.source} · ${trace.span_count || spans.length} spans · ${formatDuration(trace.duration_ms)}`;
  document.querySelector("#lite-trace-status").textContent = rootSpan?.status_code || "UNSET";
  if (!spans.length) {
    chart.innerHTML = '<div class="lite-empty-state"><div class="empty-icon">◇</div><strong>No spans stored</strong><span>This trace record does not have any persisted spans.</span></div>';
    return;
  }

  const totalMs = Math.max(Number(trace.duration_ms || 0), ...spans.map((span) => Number(span.end_offset_ms || 0)), 0.001);
  const layout = buildFlameLayout(spans);
  const canvasHeight = Math.max(82, layout.rowCount * 33 + 18);
  const blocks = layout.rows.map(({ span, row }) => {
    const left = Math.min(99.3, Math.max(0, Number(span.start_offset_ms || 0) / totalMs * 100));
    const rawWidth = Math.max(0, Number(span.duration_ms || 0) / totalMs * 100);
    const width = Math.min(100 - left, Math.max(0.7, rawWidth));
    const selected = span.span_id === activeLiteSpanId ? " selected" : "";
    const title = `${span.name} · ${formatDuration(span.duration_ms)} · ${span.span_id}`;
    return `<button class="lite-flame-block ${flameBlockTone(span)}${selected}" type="button" data-span-id="${escapeHtml(span.span_id)}" style="left:${left.toFixed(4)}%;width:${width.toFixed(4)}%;top:${row * 33 + 9}px" title="${escapeHtml(title)}">${escapeHtml(span.name)} · ${escapeHtml(formatDuration(span.duration_ms))}</button>`;
  }).join("");
  const gridlines = [25, 50, 75].map((left) => `<span class="lite-flame-gridline" style="left:${left}%"></span>`).join("");
  chart.innerHTML = `<div class="lite-flame-axis"><span>0 ms</span><span>${escapeHtml(formatDuration(totalMs / 2))}</span><span>${escapeHtml(formatDuration(totalMs))}</span></div>
    <div class="lite-flame-canvas" style="height:${canvasHeight}px">${gridlines}${blocks}</div>`;
  chart.querySelectorAll("[data-span-id]").forEach((block) => {
    block.addEventListener("click", () => {
      activeLiteSpanId = block.dataset.spanId;
      chart.querySelectorAll("[data-span-id]").forEach((candidate) => candidate.classList.toggle("selected", candidate.dataset.spanId === activeLiteSpanId));
      renderLiteSpanDetail(activeLiteSpanId);
    });
  });
};

const renderJsonBlock = (value) => {
  const hasValue = value && typeof value === "object" && Object.keys(value).length;
  if (!hasValue) return '<span class="lite-empty-json">No stored values.</span>';
  return `<pre>${escapeHtml(JSON.stringify(value, null, 2))}</pre>`;
};

const renderLiteSpanDetail = (spanId) => {
  const detail = document.querySelector("#lite-span-detail");
  const span = liteTraceDetailState?.spans?.find((candidate) => candidate.span_id === spanId);
  if (!span) {
    detail.innerHTML = '<div class="lite-empty-state compact"><strong>Span details</strong><span>Click a flame block to inspect this module.</span></div>';
    return;
  }
  const events = (span.events || []).length
    ? `<div class="lite-event-list">${span.events.map((event) => `<div class="lite-event-row"><strong>${escapeHtml(event.name)}</strong><code>+${escapeHtml(formatDuration(event.offset_ms))}</code>${renderJsonBlock(event.attributes)}</div>`).join("")}</div>`
    : '<span class="lite-empty-json">No span events.</span>';
  const llm = span.llm_call
    ? `<article class="lite-span-section"><h4>LLM projection</h4>${renderJsonBlock(span.llm_call)}</article>`
    : "";
  detail.innerHTML = `<div class="lite-span-detail-heading">
      <div><p class="section-kicker">SELECTED SPAN</p><h3>${escapeHtml(span.name)}</h3></div>
      <code>${escapeHtml(span.span_id)}</code>
    </div>
    <div class="lite-span-metrics">
      <div class="lite-span-metric"><span>Duration</span><strong>${escapeHtml(formatDuration(span.duration_ms))}</strong></div>
      <div class="lite-span-metric"><span>Start offset</span><strong>+${escapeHtml(formatDuration(span.start_offset_ms))}</strong></div>
      <div class="lite-span-metric"><span>Status</span><strong>${escapeHtml(span.status_code || "UNSET")}</strong></div>
      <div class="lite-span-metric"><span>Kind</span><strong>${escapeHtml(span.kind || "INTERNAL")}</strong></div>
    </div>
    <div class="lite-span-sections">
      <article class="lite-span-section"><h4>Input</h4>${renderJsonBlock(span.io?.input)}</article>
      <article class="lite-span-section"><h4>Output</h4>${renderJsonBlock(span.io?.output)}</article>
      ${llm}
      <article class="lite-span-section"><h4>Events</h4>${events}</article>
      <article class="lite-span-section wide"><h4>All attributes</h4>${renderJsonBlock(span.attributes)}</article>
      <article class="lite-span-section wide"><h4>Resource &amp; instrumentation</h4>${renderJsonBlock({ service_name: span.service_name, instrumentation_scope: span.instrumentation_scope, resource: span.resource, status_message: span.status_message })}</article>
    </div>`;
};

const setSkillOperationStatus = (message, tone = "") => {
  const status = document.querySelector("#skill-operation-status");
  if (!status) return;
  status.dataset.tone = tone;
  status.textContent = message;
};

const setRegisterSkillStatus = (message, tone = "") => {
  const status = document.querySelector("#register-skill-status");
  if (!status) return;
  status.dataset.tone = tone;
  status.textContent = message;
};

const setSkillContentMode = (mode) => {
  const previewMode = mode !== "edit";
  const previewButton = document.querySelector("#skill-preview-mode");
  const editButton = document.querySelector("#skill-edit-mode");
  const preview = document.querySelector("#skill-content-preview");
  const editor = document.querySelector("#skill-content-editor");
  if (!previewButton || !editButton || !preview || !editor) return;
  previewButton.classList.toggle("active", previewMode);
  editButton.classList.toggle("active", !previewMode);
  previewButton.setAttribute("aria-selected", previewMode ? "true" : "false");
  editButton.setAttribute("aria-selected", previewMode ? "false" : "true");
  preview.classList.toggle("hidden", !previewMode);
  editor.classList.toggle("hidden", previewMode);
};

const renderSkillContentPreview = (value = null) => {
  const preview = document.querySelector("#skill-content-preview");
  const editor = document.querySelector("#skill-content-editor");
  if (!preview) return;
  const source = value ?? editor?.value ?? "";
  const content = extractSkillContent(source);
  preview.innerHTML = content.trim()
    ? renderMarkdown(content)
    : '<div class="markdown-empty-state">Select a version to preview its Markdown.</div>';
};

const shortVersionId = (value) => {
  const text = String(value || "");
  return text.length > 18 ? `${text.slice(0, 12)}…` : text;
};

const versionLabel = (version) => version?.version_label || shortVersionId(version?.version_id) || "Unlabeled version";

const formatSkillDate = (value) => {
  if (!value) return "Not recorded";
  return value.replace("T", " ").replace(/Z$/, " UTC");
};

const renderSkillInfo = (payload) => {
  const record = payload.skill;
  const versions = payload.versions || [];
  const active = payload.active_version;
  const badge = document.querySelector("#skill-info-badge");
  const info = document.querySelector("#skill-info-content");
  document.querySelector("#skill-info-title").textContent = record.name;
  badge.textContent = (record.sync_state || "LOCAL_ONLY").toUpperCase();
  info.className = "skill-info-content";
  info.innerHTML = `
    <div class="detail-meta"><span>Local ID</span><code>${escapeHtml(record.skill_id)}</code></div>
    <div class="detail-meta"><span>Cloud ID</span><code>${escapeHtml(record.cloud_skill_id || "Not linked")}</code></div>
    <div class="detail-meta"><span>Active</span><code>${escapeHtml(record.active_version_id)}</code></div>
    <div class="detail-meta"><span>Published</span><code>${escapeHtml(record.published_head_id || "Not published")}</code></div>
    <div class="detail-meta"><span>Content hash</span><code>${escapeHtml(active?.content_hash || "Not calculated")}</code></div>
    <div class="detail-meta"><span>Snapshot hash</span><code>${escapeHtml(active?.local_snapshot_hash || "Not calculated")}</code></div>
    <div class="detail-meta"><span>Last sync</span><code>${escapeHtml(formatSkillDate(record.last_sync_at))}</code></div>
    <div class="skill-info-section">
      <div class="skill-info-section-heading"><span>VERSION HISTORY</span><span>${versions.length}</span></div>
      <div class="version-history-list">
        ${versions.length ? [...versions].reverse().map((version) => `
          <div class="version-history-row ${version.is_active ? "current" : ""}">
            <span class="version-history-dot"></span>
            <span><strong class="version-history-main">${escapeHtml(versionLabel(version))}</strong><small class="version-history-meta">${escapeHtml(version.status || "local_only")} · ${escapeHtml(version.sync_state)} · ${escapeHtml(formatSkillDate(version.created_at))}</small>${version.commit_message ? `<small class="version-history-meta">${escapeHtml(version.commit_message)}</small>` : ""}</span>
          </div>`).join("") : '<div class="version-history-meta">No version history recorded.</div>'}
      </div>
    </div>`;
};

const renderVersionSelector = (payload) => {
  const select = document.querySelector("#skill-version-select");
  const record = payload.skill;
  const versions = [...(payload.versions || [])];
  select.innerHTML = versions.length
    ? versions.map((version) => {
      const current = version.version_id === record.active_version_id;
      const label = current ? `Active · ${versionLabel(version)}` : versionLabel(version);
      return `<option value="${escapeHtml(version.version_id)}">${escapeHtml(label)} · ${escapeHtml(version.status || "history")}</option>`;
    }).join("")
    : '<option value="">No local versions</option>';
  select.disabled = false;
  select.value = record.active_version_id || versions.at(-1)?.version_id || "";
};

const resetSkillWorkspace = () => {
  activeSkillId = null;
  activeSkillPayload = null;
  activeVersionId = null;
  activeEditorSavedContent = "";
  document.querySelector("#skill-editor-title").textContent = "Select a skill";
  document.querySelector("#skill-editor-subtitle").textContent = "Choose a Skill to edit its current local SKILL.md.";
  document.querySelector("#skill-editor-status").textContent = "EMPTY";
  document.querySelector("#skill-version-select").innerHTML = "<option>Select a Skill first</option>";
  document.querySelector("#skill-version-select").disabled = true;
  document.querySelector("#skill-version-label").value = "";
  document.querySelector("#skill-version-label").disabled = true;
  document.querySelector("#skill-commit-message").value = "";
  document.querySelector("#skill-commit-message").disabled = true;
  document.querySelector("#skill-export-path").disabled = true;
  document.querySelector("#export-skill").disabled = true;
  document.querySelector("#sync-skill").disabled = true;
  document.querySelector("#evolve-skill").disabled = true;
  document.querySelector("#promote-skill").disabled = true;
  document.querySelector("#activate-skill-version").disabled = true;
  const editor = document.querySelector("#skill-content-editor");
  editor.value = "";
  editor.disabled = true;
  setSkillContentMode("preview");
  renderSkillContentPreview("");
  document.querySelector("#skill-info-title").textContent = "No skill selected";
  document.querySelector("#skill-info-badge").textContent = "EMPTY";
  const info = document.querySelector("#skill-info-content");
  info.className = "skill-info-empty";
  info.innerHTML = "<p>Choose a Skill to inspect its immutable versions, active pointer, hashes, and synchronization state.</p>";
  setSkillOperationStatus("Select a version to begin editing.");
  updateSkillEditorState();
};

const updateSkillEditorState = () => {
  const editor = document.querySelector("#skill-content-editor");
  const record = activeSkillPayload?.skill;
  const hasSkill = Boolean(activeSkillId && record);
  const contentChanged = hasSkill && !editor.disabled && editor.value !== activeEditorSavedContent;
  const metadataChanged = hasSkill && Boolean(
    document.querySelector("#skill-version-label").value.trim()
    || document.querySelector("#skill-commit-message").value.trim()
  );
  const hasChanges = contentChanged || metadataChanged;
  const status = document.querySelector("#skill-editor-status");
  if (hasChanges) {
    status.textContent = "DRAFT";
  } else if (record) {
    status.textContent = (record.sync_state || "LOCAL_ONLY").toUpperCase();
  }
  document.querySelector("#publish-skill").disabled = !hasChanges;
  document.querySelector("#activate-skill-version").disabled = !(
    hasSkill && activeVersionId && activeVersionId !== record.active_version_id
  );
  document.querySelector("#sync-skill").disabled = !hasSkill;
  document.querySelector("#evolve-skill").disabled = !(
    hasSkill && activeVersionId && record.cloud_skill_id
  );
  document.querySelector("#promote-skill").disabled = !(
    hasSkill
    && activeVersionId
    && record.cloud_skill_id
    && Number.isInteger(record.cloud_revision)
    && activeVersionId !== record.published_head_id
  );
};

const renderSkillDetail = (payload) => {
  if (!payload?.skill) return;
  const record = payload.skill;
  activeSkillPayload = payload;
  activeSkillId = record.skill_id;
  document.querySelector("#skill-editor-title").textContent = record.name;
  document.querySelector("#skill-editor-subtitle").textContent = "Edit a browser draft, then publish a new immutable local version.";
  document.querySelector("#skill-version-label").disabled = false;
  document.querySelector("#skill-commit-message").disabled = false;
  document.querySelector("#skill-export-path").disabled = false;
  document.querySelector("#export-skill").disabled = false;
  renderVersionSelector(payload);
  renderSkillInfo(payload);
  document.querySelectorAll("[data-skill-ref]").forEach((button) => {
    button.classList.toggle("active", button.dataset.skillRef === activeSkillId);
  });
  updateSkillEditorState();
};

const loadSkillDetail = async (skillRef) => {
  const editor = document.querySelector("#skill-content-editor");
  if (activeSkillId && editor.value !== activeEditorSavedContent && activeSkillId !== skillRef) {
    if (!window.confirm("You have unsaved Skill edits. Switch skills and discard them?")) return;
  }
  activeSkillId = skillRef;
  activeSkillPayload = null;
  activeVersionId = null;
  activeEditorSavedContent = "";
  editor.value = "";
  editor.disabled = true;
  setSkillContentMode("preview");
  renderSkillContentPreview("");
  try {
    const payload = await apiRequest(`/api/v1/skills/${encodeURIComponent(skillRef)}`);
    renderSkillDetail(payload);
    const defaultVersion = payload.skill.active_version_id || payload.versions?.at(-1)?.version_id || null;
    await loadSkillVersion(defaultVersion, { force: true });
    return payload;
  } catch (error) {
    setSkillOperationStatus(`Unable to load Skill: ${error.message}`, "error");
    return null;
  }
};

const loadSkillVersion = async (versionId, { force = false } = {}) => {
  if (!activeSkillId) return;
  const editor = document.querySelector("#skill-content-editor");
  if (!force && editor.value !== activeEditorSavedContent) {
    if (!window.confirm("You have unsaved Skill edits. Switch versions and discard them?")) {
      document.querySelector("#skill-version-select").value = activeVersionId || "";
      return;
    }
  }
  try {
    const query = versionId ? `?version_id=${encodeURIComponent(versionId)}` : "";
    const payload = await apiRequest(`/api/v1/skills/${encodeURIComponent(activeSkillId)}/content${query}`);
    activeVersionId = payload.version_id || versionId || null;
    activeEditorSavedContent = extractSkillContent(payload.content);
    editor.value = activeEditorSavedContent;
    editor.disabled = false;
    setSkillContentMode("preview");
    renderSkillContentPreview(activeEditorSavedContent);
    document.querySelector("#skill-version-label").disabled = false;
    setSkillOperationStatus(`Editing a draft based on ${shortVersionId(activeVersionId)}. Publish creates a new immutable version.`);
    updateSkillEditorState();
  } catch (error) {
    setSkillOperationStatus(`Unable to load version: ${error.message}`, "error");
  }
};

const extractSkillContent = (value) => {
  if (Array.isArray(value)) {
    const skillFile = value.find((file) => file && file.path === "SKILL.md");
    return typeof skillFile?.content === "string" ? skillFile.content : "";
  }
  if (value && typeof value === "object") {
    return typeof value.content === "string" ? value.content : "";
  }
  if (typeof value !== "string") return "";

  const trimmed = value.trim();
  if (!trimmed) return "";
  try {
    const parsed = JSON.parse(trimmed);
    if (Array.isArray(parsed)) return extractSkillContent(parsed);
    if (parsed && typeof parsed === "object" && typeof parsed.content === "string") {
      return parsed.content;
    }
  } catch {
    // A plain SKILL.md is expected from the local UI API; keep it as-is.
  }
  return value;
};

const renderCompareContent = () => {
  const canCompare = !comparePaneMessages.left
    && !comparePaneMessages.right
    && compareContentState.left.trim()
    && compareContentState.right.trim();
  const diffSources = canCompare
    ? buildMarkdownDiffSources(compareContentState.left, compareContentState.right)
    : null;
  ["left", "right"].forEach((side) => {
    const pane = document.querySelector(`#compare-${side}`);
    if (!pane) return;
    const message = comparePaneMessages[side];
    const content = compareContentState[side];
    const renderedSource = diffSources?.[side] || content;
    pane.innerHTML = message
      ? `<div class="markdown-empty-state">${escapeHtml(message)}</div>`
      : (content.trim() ? renderMarkdown(renderedSource) : '<div class="markdown-empty-state">No SKILL.md content.</div>');
  });
};

const compareSideSelectors = {
  left: { skill: "#compare-left-select", version: "#compare-left-version-select" },
  right: { skill: "#compare-right-select", version: "#compare-right-version-select" },
};

const renderCompareVersionOptions = (side, payload, { preferredVersionId = null } = {}) => {
  const versionSelect = document.querySelector(compareSideSelectors[side].version);
  const record = payload.skill;
  const versions = [...(payload.versions || [])];
  versionSelect.innerHTML = versions.length
    ? versions.map((version) => {
      const current = version.version_id === record.active_version_id;
      const label = current ? `Active · ${versionLabel(version)}` : versionLabel(version);
      return `<option value="${escapeHtml(version.version_id)}">${escapeHtml(label)} · ${escapeHtml(version.status || "history")}</option>`;
    }).join("")
    : '<option value="">No local versions</option>';
  versionSelect.disabled = false;
  const availableVersionIds = new Set(versions.map((version) => version.version_id));
  const selectedVersionId = preferredVersionId && availableVersionIds.has(preferredVersionId)
    ? preferredVersionId
    : (record.active_version_id || versions.at(-1)?.version_id || "");
  versionSelect.value = selectedVersionId;
  return selectedVersionId;
};

const loadSkillContent = async (side) => {
  const selectors = compareSideSelectors[side];
  const skillSelect = document.querySelector(selectors.skill);
  const versionSelect = document.querySelector(selectors.version);
  const requestToken = ++compareRequestTokens[side];
  if (!skillSelect?.value) {
    compareContentState[side] = "";
    comparePaneMessages[side] = "Select a Skill to load its content.";
    renderCompareContent();
    return;
  }

  compareContentState[side] = "";
  comparePaneMessages[side] = "Loading SKILL.md…";
  renderCompareContent();
  try {
    const versionId = versionSelect?.value || "";
    const query = versionId ? `?version_id=${encodeURIComponent(versionId)}` : "";
    const payload = await apiRequest(`/api/v1/skills/${encodeURIComponent(skillSelect.value)}/content${query}`);
    if (requestToken !== compareRequestTokens[side]) return;
    compareContentState[side] = extractSkillContent(payload.content);
    comparePaneMessages[side] = "";
    renderCompareContent();
  } catch (error) {
    if (requestToken !== compareRequestTokens[side]) return;
    compareContentState[side] = "";
    comparePaneMessages[side] = `Unable to load Skill content: ${error.message}`;
    renderCompareContent();
  }
};

const loadCompareVersions = async (side, { preserveSelection = false } = {}) => {
  const selectors = compareSideSelectors[side];
  const skillSelect = document.querySelector(selectors.skill);
  const versionSelect = document.querySelector(selectors.version);
  const preferredVersionId = preserveSelection ? versionSelect.value : null;
  const requestToken = ++compareVersionRequestTokens[side];
  if (!skillSelect?.value) {
    versionSelect.innerHTML = "<option>Select a Skill first</option>";
    versionSelect.disabled = true;
    compareContentState[side] = "";
    comparePaneMessages[side] = "Select a Skill to load its content.";
    renderCompareContent();
    return;
  }

  versionSelect.innerHTML = "<option>Loading versions…</option>";
  versionSelect.disabled = true;
  compareContentState[side] = "";
  comparePaneMessages[side] = "Loading Skill versions…";
  renderCompareContent();
  try {
    const payload = await apiRequest(`/api/v1/skills/${encodeURIComponent(skillSelect.value)}`);
    if (requestToken !== compareVersionRequestTokens[side]) return;
    renderCompareVersionOptions(side, payload, { preferredVersionId });
    await loadSkillContent(side);
  } catch (error) {
    if (requestToken !== compareVersionRequestTokens[side]) return;
    versionSelect.innerHTML = "<option>Unable to load versions</option>";
    versionSelect.disabled = true;
    compareContentState[side] = "";
    comparePaneMessages[side] = `Unable to load Skill versions: ${error.message}`;
    renderCompareContent();
  }
};

const refreshCompareForSkill = async (skillId, payload = null) => {
  const matchingSides = ["left", "right"].filter((side) => {
    const skillSelect = document.querySelector(compareSideSelectors[side].skill);
    return skillSelect?.value === skillId;
  });
  await Promise.all(matchingSides.map(async (side) => {
    if (!payload) {
      await loadCompareVersions(side, { preserveSelection: true });
      return;
    }
    const versionSelect = document.querySelector(compareSideSelectors[side].version);
    const previousVersionId = versionSelect?.value || "";
    const selectedVersionId = renderCompareVersionOptions(
      side,
      payload,
      { preferredVersionId: previousVersionId },
    );
    if (selectedVersionId !== previousVersionId || !compareContentState[side]) {
      await loadSkillContent(side);
    }
  }));
};

const renderSkills = (payload) => {
  skillsState = payload.skills || [];
  document.querySelector("#overview-skills-value").textContent = `${skillsState.length} registered`;
  document.querySelector("#overview-skills-detail").textContent = payload.pending_count
    ? `${payload.pending_count} pending sync operation${payload.pending_count === 1 ? "" : "s"}`
    : "No pending sync operations";
  const list = document.querySelector("#skills-list");
  if (!skillsState.length) {
    list.className = "empty-list";
    list.innerHTML = '<div class="empty-icon small">✦</div><strong>No skills registered</strong><span>Registered local Skills will appear here.</span>';
  } else {
    list.className = "skill-list";
    list.innerHTML = skillsState.map((skill) => `
      <button class="skill-list-row" type="button" data-skill-ref="${escapeHtml(skill.skill_id)}">
        <span class="skill-list-mark"></span>
        <span><strong>${escapeHtml(skill.name)}</strong><small>${escapeHtml(shortVersionId(skill.active_version_id) || "Unversioned")}</small></span>
        <span class="skill-state">${escapeHtml(skill.sync_state || "local_only")}</span>
      </button>`).join("");
    list.querySelectorAll("[data-skill-ref]").forEach((button) => {
      button.addEventListener("click", () => loadSkillDetail(button.dataset.skillRef));
    });
  }

  const options = skillsState.length
    ? skillsState.map((skill) => `<option value="${escapeHtml(skill.skill_id)}">${escapeHtml(skill.name)} · ${escapeHtml(shortVersionId(skill.active_version_id) || "local")}</option>`).join("")
    : "<option value=\"\">No registered skills</option>";
  ["compare-left-select", "compare-right-select"].forEach((id) => {
    const select = document.querySelector(`#${id}`);
    select.innerHTML = options;
    select.disabled = !skillsState.length;
  });
  ["compare-left-version-select", "compare-right-version-select"].forEach((id) => {
    const select = document.querySelector(`#${id}`);
    select.innerHTML = skillsState.length ? "<option>Loading versions…</option>" : "<option>Select a Skill first</option>";
    select.disabled = true;
  });
  if (skillsState.length) {
    document.querySelector("#compare-left-select").value = skillsState[0].skill_id;
    document.querySelector("#compare-right-select").value = (skillsState[1] || skillsState[0]).skill_id;
    Promise.all([
      loadCompareVersions("left"),
      loadCompareVersions("right"),
    ]);
    const selectedSkill = activeSkillId && skillsState.some((skill) => skill.skill_id === activeSkillId)
      ? activeSkillId
      : skillsState[0].skill_id;
    loadSkillDetail(selectedSkill);
  } else {
    resetSkillWorkspace();
    compareContentState.left = "";
    compareContentState.right = "";
    comparePaneMessages.left = "Select a Skill to load its content.";
    comparePaneMessages.right = "Select a Skill to load its content.";
    document.querySelector("#compare-left-version-select").innerHTML = "<option>Select a Skill first</option>";
    document.querySelector("#compare-right-version-select").innerHTML = "<option>Select a Skill first</option>";
    renderCompareContent();
  }
};

const loadConfig = async () => {
  try {
    renderConfig(await apiRequest("/api/v1/config"));
  } catch (error) {
    document.querySelector("#preview-badge").textContent = "PREVIEW · OFFLINE";
    setNotice(`Local SDK API is unavailable: ${error.message}`, "error");
  }
};

const loadSkills = async () => {
  try {
    renderSkills(await apiRequest("/api/v1/skills"));
  } catch (error) {
    document.querySelector("#overview-skills-value").textContent = "Unavailable";
    document.querySelector("#overview-skills-detail").textContent = error.message;
  }
};

const updateActiveSkillListRow = (record, pendingCount = 0) => {
  skillsState = skillsState.map((skill) => skill.skill_id === record.skill_id ? { ...skill, ...record } : skill);
  document.querySelectorAll("[data-skill-ref]").forEach((button) => {
    if (button.dataset.skillRef !== record.skill_id) return;
    const version = button.querySelector("small");
    const state = button.querySelector(".skill-state");
    if (version) version.textContent = shortVersionId(record.active_version_id) || "Unversioned";
    if (state) state.textContent = record.sync_state || "local_only";
    button.classList.add("active");
  });
  document.querySelector("#overview-skills-detail").textContent = pendingCount
    ? `${pendingCount} pending sync operation${pendingCount === 1 ? "" : "s"}`
    : "No pending sync operations";
};

const publishSkillContent = async () => {
  if (!activeSkillId) return;
  const editor = document.querySelector("#skill-content-editor");
  const content = editor.value;
  if (!content.trim()) {
    setSkillOperationStatus("Skill content cannot be empty.", "error");
    return;
  }

  const publishButton = document.querySelector("#publish-skill");
  publishButton.disabled = true;
  setSkillOperationStatus("Publishing a new immutable local version…");
  const label = document.querySelector("#skill-version-label").value.trim();
  const commitMessage = document.querySelector("#skill-commit-message").value.trim();
  const body = {
    content,
    base_version_id: activeVersionId,
    version_label: label || null,
    commit_message: commitMessage || null,
    activate: document.querySelector("#activate-published-skill").checked,
  };
  const endpoint = `/api/v1/skills/${encodeURIComponent(activeSkillId)}/publish`;
  try {
    const payload = await apiRequest(endpoint, {
      method: "POST",
      body: JSON.stringify(body),
    });
    renderSkillDetail(payload.detail);
    await loadSkillVersion(payload.result.version_id, { force: true });
    updateActiveSkillListRow(payload.detail.skill, payload.detail.outbox_operations?.length || 0);
    await refreshCompareForSkill(payload.detail.skill.skill_id, payload.detail);
    document.querySelector("#skill-version-label").value = "";
    document.querySelector("#skill-commit-message").value = "";
    updateSkillEditorState();
    setSkillOperationStatus(payload.message || "Published a new immutable Skill version.", "success");
  } catch (error) {
    setSkillOperationStatus(`Unable to publish: ${error.message}`, "error");
    updateSkillEditorState();
  }
};

const activateSelectedSkillVersion = async () => {
  if (!activeSkillId || !activeVersionId) return;
  try {
    const payload = await apiRequest(`/api/v1/skills/${encodeURIComponent(activeSkillId)}/switch`, {
      method: "POST",
      body: JSON.stringify({ version_id: activeVersionId }),
    });
    renderSkillDetail(payload);
    updateActiveSkillListRow(payload.skill, payload.outbox_operations?.length || 0);
    await refreshCompareForSkill(payload.skill.skill_id, payload);
    setSkillOperationStatus(`Activated ${shortVersionId(activeVersionId)} without changing version files.`, "success");
  } catch (error) {
    setSkillOperationStatus(`Unable to activate version: ${error.message}`, "error");
  }
};

const exportSelectedSkillVersion = async () => {
  if (!activeSkillId || !activeVersionId) return;
  const targetPath = document.querySelector("#skill-export-path").value.trim();
  if (!targetPath) {
    setSkillOperationStatus("Enter an absolute export directory.", "error");
    return;
  }
  try {
    const payload = await apiRequest(`/api/v1/skills/${encodeURIComponent(activeSkillId)}/export`, {
      method: "POST",
      body: JSON.stringify({ target_path: targetPath, version_id: activeVersionId, replace: true }),
    });
    setSkillOperationStatus(`Exported ${payload.exported_files.length} files to ${payload.target_path}.`, "success");
  } catch (error) {
    setSkillOperationStatus(`Unable to export: ${error.message}`, "error");
  }
};

const syncSelectedSkill = async () => {
  if (!activeSkillId) return;
  setSkillOperationStatus("Synchronizing immutable versions and cloud pointers…");
  try {
    const payload = await apiRequest(`/api/v1/skills/${encodeURIComponent(activeSkillId)}/sync`, {
      method: "POST",
      body: JSON.stringify({ direction: "both" }),
    });
    renderSkillDetail(payload);
    updateActiveSkillListRow(payload.skill, payload.outbox_operations?.length || 0);
    const selected = activeVersionId && payload.versions.some((item) => item.version_id === activeVersionId)
      ? activeVersionId
      : payload.skill.active_version_id;
    await loadSkillVersion(selected, { force: true });
    await refreshCompareForSkill(payload.skill.skill_id, payload);
    setSkillOperationStatus("Cloud synchronization completed without changing the active pointer.", "success");
  } catch (error) {
    setSkillOperationStatus(`Unable to sync: ${error.message}`, "error");
  }
};

const evolveSelectedSkill = async () => {
  if (!activeSkillId || !activeVersionId) return;
  setSkillOperationStatus("Requesting cloud evolution from the selected version…");
  try {
    const payload = await apiRequest(`/api/v1/skills/${encodeURIComponent(activeSkillId)}/evolve`, {
      method: "POST",
      body: JSON.stringify({
        base_version_id: activeVersionId,
        mode: "sync",
        operation_id: crypto.randomUUID(),
      }),
    });
    const result = payload.evolved
      ? `Evolution created ${payload.new_version_ids.length} cloud draft version(s). Sync to download them.`
      : `No version created; ${payload.pending_count}/${payload.threshold} traces are ready.`;
    setSkillOperationStatus(result, "success");
  } catch (error) {
    setSkillOperationStatus(`Unable to evolve: ${error.message}`, "error");
  }
};

const promoteSelectedSkill = async () => {
  const record = activeSkillPayload?.skill;
  if (!activeSkillId || !activeVersionId || !record) return;
  const promotedVersionId = activeVersionId;
  setSkillOperationStatus("Promoting the selected cloud version…");
  try {
    await apiRequest(`/api/v1/skills/${encodeURIComponent(activeSkillId)}/promote`, {
      method: "POST",
      body: JSON.stringify({
        version_id: promotedVersionId,
        expected_cloud_revision: record.cloud_revision,
        operation_id: crypto.randomUUID(),
      }),
    });
    const detail = await loadSkillDetail(activeSkillId);
    if (detail) await refreshCompareForSkill(detail.skill.skill_id, detail);
    setSkillOperationStatus(
      `Promoted ${shortVersionId(promotedVersionId)} without changing the local active pointer.`,
      "success",
    );
  } catch (error) {
    setSkillOperationStatus(`Unable to promote: ${error.message}`, "error");
  }
};

const registerSkill = async () => {
  const sourcePath = document.querySelector("#register-skill-path").value.trim();
  if (!sourcePath) {
    setRegisterSkillStatus("Enter a Skill directory or SKILL.md path.", "error");
    return;
  }
  setRegisterSkillStatus("");
  const alias = document.querySelector("#register-skill-alias").value.trim();
  const versionLabel = document.querySelector("#register-skill-version-label").value.trim();
  const commitMessage = document.querySelector("#register-skill-message").value.trim();
  const duplicateAction = document.querySelector("#register-skill-duplicate-action").value;
  try {
    const result = await apiRequest("/api/v1/skills/register", {
      method: "POST",
      body: JSON.stringify({
        source_path: sourcePath,
        alias: alias || null,
        version_label: versionLabel || null,
        commit_message: commitMessage || null,
        duplicate_action: duplicateAction || null,
      }),
    });
    document.querySelector("#register-skill-path").value = "";
    document.querySelector("#register-skill-alias").value = "";
    document.querySelector("#register-skill-version-label").value = "";
    document.querySelector("#register-skill-message").value = "";
    document.querySelector("#register-skill-duplicate-action").value = "";
    await loadSkills();
    await loadSkillDetail(result.skill_id);
    const verb = result.action === "reused" ? "Reused" : "Registered";
    setRegisterSkillStatus(`${verb} local version ${shortVersionId(result.version_id)}.`, "success");
  } catch (error) {
    const duplicateHint = error.message.includes("identical local Skill snapshot already exists")
      ? 'An identical Skill is already registered. To create another Skill, change "If an identical snapshot already exists" to "Register a separate Skill", then click Register again.'
      : `Unable to register: ${error.message}`;
    setRegisterSkillStatus(duplicateHint, "error");
  }
};

const saveConfig = async () => {
  const memory = configState?.memory || {};
  const apiKey = document.querySelector("#setting-api-key").value;
  let searchFilters;
  let getFilters;
  try {
    searchFilters = parseJsonObject("setting-search-filters", "Search filters");
    getFilters = parseJsonObject("setting-get-filters", "Get filters");
  } catch (error) {
    setNotice(error.message, "error");
    return;
  }
  const payload = {
    base_url: document.querySelector("#setting-base-url").value,
    api_key: apiKey,
    user_id: document.querySelector("#setting-user-id").value,
    app_id: document.querySelector("#setting-app-id").value,
    agent_id: document.querySelector("#setting-agent-id").value,
    session_id: document.querySelector("#setting-session-id").value,
    skill_cache_dir: document.querySelector("#setting-cache-dir").value,
    skill_backup_dir: document.querySelector("#setting-backup-dir").value,
    timeout_seconds: Number(document.querySelector("#setting-timeout").value),
    max_retries: Number(document.querySelector("#setting-retries").value),
    memory: {
      ...memory,
      search_top_k: Number(document.querySelector("#setting-search-top-k").value) || null,
      search_strategy: document.querySelector("#setting-search-strategy").value,
      search_rerank: document.querySelector("#setting-search-rerank").value === "true",
      search_score_threshold: numberOrNull("setting-search-score-threshold"),
      search_filters: searchFilters,
      get_top_k: numberOrNull("setting-get-top-k"),
      get_filters: getFilters,
      feedback_mode: document.querySelector("#setting-feedback-mode").value || null,
      add_mode: document.querySelector("#setting-add-mode").value,
      add_default_role: document.querySelector("#setting-add-role").value,
      add_auto_skill_context: document.querySelector("#setting-auto-skill-context").checked,
      dreaming_mode: document.querySelector("#setting-dreaming-mode").value,
    },
  };
  const button = document.querySelector("#save-settings");
  button.disabled = true;
  try {
    renderConfig(await apiRequest("/api/v1/config", { method: "PUT", body: JSON.stringify(payload) }));
    memoryLoaded = false;
    setNotice("Configuration saved atomically.", "success");
  } catch (error) {
    setNotice(`Unable to save configuration: ${error.message}`, "error");
  } finally {
    button.disabled = false;
  }
};

const numberOrNull = (id) => {
  const value = document.querySelector(`#${id}`).value.trim();
  return value === "" ? null : Number(value);
};

const parseJsonObject = (id, label) => {
  const value = document.querySelector(`#${id}`).value.trim();
  if (!value) return {};
  try {
    const parsed = JSON.parse(value);
    if (!parsed || Array.isArray(parsed) || typeof parsed !== "object") throw new Error();
    return parsed;
  } catch {
    throw new Error(`${label} must be a JSON object.`);
  }
};

const DEMO_LEFT = `---
name: writing-assistant
version: 1.0.0
---

# Writing assistant

Help the user write clear and concise text.

## Guidelines
- Ask for the intended audience.
- Keep the final answer concise.`;

const DEMO_RIGHT = `---
name: writing-assistant
version: 1.1.0
---

# Writing assistant

Help the user write clear, concise, and useful text.

## Guidelines
- Ask for the intended audience.
- Preserve the user's voice.
- Keep the final answer concise.`;

const escapeHtml = (value) => String(value)
  .replaceAll("&", "&amp;")
  .replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;")
  .replaceAll("'", "&#039;");

const safeMarkdownHref = (value) => {
  const href = String(value).trim();
  return /^(?:https?:\/\/|mailto:|#|\/|\.\.?\/)/i.test(href) ? escapeHtml(href) : "";
};

const DIFF_MARKER_PATTERN = /\u0001md-diff-(added|removed)-start\u0001([\s\S]*?)\u0001md-diff-end\u0001/g;

const makeDiffMarker = (kind, value) => `\u0001md-diff-${kind}-start\u0001${value}\u0001md-diff-end\u0001`;

const pushDiffOperation = (operations, type, items) => {
  if (!items.length) return;
  const previous = operations.at(-1);
  if (previous?.type === type) {
    previous.items.push(...items);
  } else {
    operations.push({ type, items: [...items] });
  }
};

const diffSequence = (leftItems, rightItems, equals, maxCells = 2500000) => {
  const left = Array.from(leftItems);
  const right = Array.from(rightItems);
  if (!left.length) return right.length ? [{ type: "insert", items: right }] : [];
  if (!right.length) return [{ type: "delete", items: left }];

  if (left.length * right.length > maxCells) {
    let prefixLength = 0;
    while (prefixLength < left.length && prefixLength < right.length && equals(left[prefixLength], right[prefixLength])) {
      prefixLength += 1;
    }
    let suffixLength = 0;
    while (
      suffixLength < left.length - prefixLength
      && suffixLength < right.length - prefixLength
      && equals(left[left.length - suffixLength - 1], right[right.length - suffixLength - 1])
    ) {
      suffixLength += 1;
    }
    const operations = [];
    pushDiffOperation(operations, "equal", left.slice(0, prefixLength));
    pushDiffOperation(operations, "delete", left.slice(prefixLength, left.length - suffixLength));
    pushDiffOperation(operations, "insert", right.slice(prefixLength, right.length - suffixLength));
    pushDiffOperation(operations, "equal", right.slice(right.length - suffixLength));
    return operations;
  }

  const rows = Array.from({ length: left.length + 1 }, () => new Uint32Array(right.length + 1));
  for (let leftIndex = left.length - 1; leftIndex >= 0; leftIndex -= 1) {
    const row = rows[leftIndex];
    const nextRow = rows[leftIndex + 1];
    for (let rightIndex = right.length - 1; rightIndex >= 0; rightIndex -= 1) {
      row[rightIndex] = equals(left[leftIndex], right[rightIndex])
        ? nextRow[rightIndex + 1] + 1
        : Math.max(nextRow[rightIndex], row[rightIndex + 1]);
    }
  }

  const operations = [];
  let leftIndex = 0;
  let rightIndex = 0;
  while (leftIndex < left.length && rightIndex < right.length) {
    if (equals(left[leftIndex], right[rightIndex])) {
      pushDiffOperation(operations, "equal", [left[leftIndex]]);
      leftIndex += 1;
      rightIndex += 1;
    } else if (rows[leftIndex + 1][rightIndex] >= rows[leftIndex][rightIndex + 1]) {
      pushDiffOperation(operations, "delete", [left[leftIndex]]);
      leftIndex += 1;
    } else {
      pushDiffOperation(operations, "insert", [right[rightIndex]]);
      rightIndex += 1;
    }
  }
  pushDiffOperation(operations, "delete", left.slice(leftIndex));
  pushDiffOperation(operations, "insert", right.slice(rightIndex));
  return operations;
};

const tokenizeDiffWords = (value) => String(value).match(/\s+|[A-Za-z0-9_]+|[^\sA-Za-z0-9_]/g) || [];

const renderDiffWordPair = (leftValue, rightValue) => {
  const operations = diffSequence(
    tokenizeDiffWords(leftValue),
    tokenizeDiffWords(rightValue),
    (leftToken, rightToken) => leftToken === rightToken,
    120000,
  );
  const sides = { left: [], right: [] };
  operations.forEach((operation) => {
    const text = operation.items.join("");
    if (operation.type === "equal") {
      sides.left.push(text);
      sides.right.push(text);
    } else if (operation.type === "delete") {
      sides.left.push(makeDiffMarker("removed", text));
    } else {
      sides.right.push(makeDiffMarker("added", text));
    }
  });
  return { left: sides.left.join(""), right: sides.right.join("") };
};

const splitMarkdownLine = (line) => {
  const text = String(line);
  const match = text.match(/^(\s{0,3}(?:#{1,6}\s+|>\s?|[-+*]\s+|\d+[.)]\s+))(.*)$/);
  return match ? { prefix: match[1], body: match[2] } : { prefix: "", body: text };
};

const decorateDiffLine = (line, kind) => {
  const text = String(line);
  if (!text.trim() || /^\s*```/.test(text) || /^\s*(?:[-*_]\s*){3,}$/.test(text)) return text;
  const { prefix, body } = splitMarkdownLine(text);
  return `${prefix}${body ? makeDiffMarker(kind, body) : body}`;
};

const renderDiffLinePair = (leftLine, rightLine) => {
  const leftParts = splitMarkdownLine(leftLine);
  const rightParts = splitMarkdownLine(rightLine);
  if (leftParts.prefix !== rightParts.prefix) {
    return {
      left: decorateDiffLine(leftLine, "removed"),
      right: decorateDiffLine(rightLine, "added"),
    };
  }
  const bodyPair = renderDiffWordPair(leftParts.body, rightParts.body);
  return {
    left: `${leftParts.prefix}${bodyPair.left}`,
    right: `${rightParts.prefix}${bodyPair.right}`,
  };
};

const buildMarkdownDiffSources = (leftSource, rightSource) => {
  const leftLines = String(leftSource).replace(/\r\n?/g, "\n").split("\n");
  const rightLines = String(rightSource).replace(/\r\n?/g, "\n").split("\n");
  const operations = diffSequence(leftLines, rightLines, (leftLine, rightLine) => leftLine === rightLine);
  const sides = { left: [], right: [] };

  let index = 0;
  while (index < operations.length) {
    const operation = operations[index];
    if (operation.type === "equal") {
      sides.left.push(...operation.items);
      sides.right.push(...operation.items);
      index += 1;
      continue;
    }

    if (operation.type === "delete") {
      const next = operations[index + 1];
      const inserted = next?.type === "insert" ? next.items : [];
      const pairCount = Math.min(operation.items.length, inserted.length);
      for (let pairIndex = 0; pairIndex < pairCount; pairIndex += 1) {
        const pair = renderDiffLinePair(operation.items[pairIndex], inserted[pairIndex]);
        sides.left.push(pair.left);
        sides.right.push(pair.right);
      }
      sides.left.push(...operation.items.slice(pairCount).map((line) => decorateDiffLine(line, "removed")));
      sides.right.push(...inserted.slice(pairCount).map((line) => decorateDiffLine(line, "added")));
      index += inserted.length ? 2 : 1;
      continue;
    }

    sides.right.push(...operation.items.map((line) => decorateDiffLine(line, "added")));
    index += 1;
  }

  return { left: sides.left.join("\n"), right: sides.right.join("\n") };
};

const renderInlineMarkdown = (value) => {
  const placeholders = [];
  const stash = (html) => {
    const token = `\u0000${placeholders.length}\u0000`;
    placeholders.push(html);
    return token;
  };

  let text = String(value);
  text = text.replace(DIFF_MARKER_PATTERN, (_match, kind, diffText) => stash(
    `<span class="markdown-diff markdown-diff-${kind}">${renderInlineMarkdown(diffText)}</span>`,
  ));
  text = text.replace(/`([^`\n]+)`/g, (_match, code) => stash(`<code>${escapeHtml(code)}</code>`));
  text = text.replace(/\[([^\]\n]+)\]\((\S+?)(?:\s+"([^"]*)")?\)/g, (_match, label, rawHref, title) => {
    const href = safeMarkdownHref(rawHref);
    if (!href) return escapeHtml(label);
    const titleAttribute = title ? ` title="${escapeHtml(title)}"` : "";
    return stash(`<a href="${href}"${titleAttribute} target="_blank" rel="noreferrer">${renderInlineMarkdown(label)}</a>`);
  });
  text = escapeHtml(text);
  text = text.replace(/\*\*(.+?)\*\*|__(.+?)__/g, (_match, strongA, strongB) => `<strong>${strongA || strongB}</strong>`);
  text = text.replace(/~~(.+?)~~/g, (_match, content) => `<del>${content}</del>`);
  text = text.replace(/(^|[^\w*])\*([^*\n]+)\*(?!\*)/g, (_match, prefix, content) => `${prefix}<em>${content}</em>`);
  text = text.replace(/(^|[^\w_])_([^_\n]+)_(?!\w)/g, (_match, prefix, content) => `${prefix}<em>${content}</em>`);
  return text.replace(/\u0000(\d+)\u0000/g, (_match, index) => placeholders[Number(index)] || "");
};

const renderCodeContent = (value) => {
  const placeholders = [];
  const marked = String(value).replace(DIFF_MARKER_PATTERN, (_match, kind, diffText) => {
    const token = `\u0000${placeholders.length}\u0000`;
    placeholders.push(`<span class="markdown-diff markdown-diff-${kind}">${escapeHtml(diffText)}</span>`);
    return token;
  });
  return escapeHtml(marked).replace(/\u0000(\d+)\u0000/g, (_match, index) => placeholders[Number(index)] || "");
};

const renderMarkdown = (source) => {
  const lines = String(source).replace(/\r\n?/g, "\n").split("\n");
  const blocks = [];
  let paragraph = [];
  let listType = null;
  let listItems = [];
  let quoteLines = [];

  const flushParagraph = () => {
    if (!paragraph.length) return;
    blocks.push(`<p>${paragraph.map((line) => renderInlineMarkdown(line)).join("<br />")}</p>`);
    paragraph = [];
  };

  const flushList = () => {
    if (!listItems.length) return;
    const tag = listType === "ordered" ? "ol" : "ul";
    blocks.push(`<${tag}>${listItems.map((item) => `<li>${renderInlineMarkdown(item)}</li>`).join("")}</${tag}>`);
    listType = null;
    listItems = [];
  };

  const flushQuote = () => {
    if (!quoteLines.length) return;
    blocks.push(`<blockquote>${renderMarkdown(quoteLines.join("\n"))}</blockquote>`);
    quoteLines = [];
  };

  const flushOpenBlocks = () => {
    flushParagraph();
    flushList();
    flushQuote();
  };

  let index = 0;
  while (index < lines.length) {
    const line = lines[index];
    const fence = line.match(/^\s*```\s*([\w-]*)\s*$/);
    if (fence) {
      flushOpenBlocks();
      const language = fence[1] ? ` class="language-${escapeHtml(fence[1])}"` : "";
      const codeLines = [];
      index += 1;
      while (index < lines.length && !/^\s*```\s*$/.test(lines[index])) {
        codeLines.push(lines[index]);
        index += 1;
      }
      if (index < lines.length) index += 1;
      blocks.push(`<pre><code${language}>${renderCodeContent(codeLines.join("\n"))}</code></pre>`);
      continue;
    }

    if (!line.trim()) {
      flushOpenBlocks();
      index += 1;
      continue;
    }

    const heading = line.match(/^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$/);
    if (heading) {
      flushOpenBlocks();
      const level = heading[1].length;
      blocks.push(`<h${level}>${renderInlineMarkdown(heading[2])}</h${level}>`);
      index += 1;
      continue;
    }

    if (/^\s{0,3}(?:[-*_]\s*){3,}$/.test(line)) {
      flushOpenBlocks();
      blocks.push("<hr />");
      index += 1;
      continue;
    }

    const quote = line.match(/^\s{0,3}>\s?(.*)$/);
    if (quote) {
      flushParagraph();
      flushList();
      quoteLines.push(quote[1]);
      index += 1;
      continue;
    }
    if (quoteLines.length) flushQuote();

    const unorderedItem = line.match(/^\s{0,3}[-+*]\s+(.+)$/);
    const orderedItem = line.match(/^\s{0,3}\d+[.)]\s+(.+)$/);
    if (unorderedItem || orderedItem) {
      flushParagraph();
      const nextType = orderedItem ? "ordered" : "unordered";
      if (listType && listType !== nextType) flushList();
      listType = nextType;
      listItems.push((orderedItem || unorderedItem)[1]);
      index += 1;
      continue;
    }
    if (listItems.length) flushList();

    paragraph.push(line);
    index += 1;
  }
  flushOpenBlocks();
  return blocks.join("");
};

const setSkillView = (viewName) => {
  document.querySelectorAll("[data-skill-view]").forEach((tab) => {
    const active = tab.dataset.skillView === viewName;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", active ? "true" : "false");
  });
  document.querySelectorAll("[data-skill-panel]").forEach((panel) => {
    panel.classList.toggle("hidden", panel.dataset.skillPanel !== viewName);
  });
  if (viewName === "compare") {
    document.querySelector("#skills-page")?.scrollIntoView({ block: "start" });
    Promise.all([
      loadCompareVersions("left", { preserveSelection: true }),
      loadCompareVersions("right", { preserveSelection: true }),
    ]);
  }
};

const setLiteView = (viewName) => {
  const activeView = viewName === "runtime" ? "runtime" : "traces";
  document.querySelectorAll("[data-lite-view]").forEach((tab) => {
    const active = tab.dataset.liteView === activeView;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", active ? "true" : "false");
  });
  document.querySelectorAll("[data-lite-panel]").forEach((panel) => {
    panel.classList.toggle("hidden", panel.dataset.litePanel !== activeView);
  });
};

const setActiveTab = (tabName, { updateHistory = true } = {}) => {
  const activeTab = TAB_COPY[tabName] ? tabName : "overview";
  const copy = TAB_COPY[activeTab];
  document.querySelectorAll("[data-tab]").forEach((tab) => {
    const active = tab.dataset.tab === activeTab;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-current", active ? "page" : "false");
  });
  document.querySelectorAll("[data-page]").forEach((page) => {
    page.classList.toggle("hidden", page.dataset.page !== activeTab);
  });
  document.querySelector("#page-title").textContent = copy.title;
  document.querySelector("#page-description").textContent = copy.description;
  document.querySelector("#save-settings")?.classList.toggle("hidden", activeTab !== "settings");
  if (activeTab === "memory" && !memoryLoaded) loadMemories();
  if (updateHistory && window.location.hash !== `#${activeTab}`) {
    history.pushState(null, "", `#${activeTab}`);
  }
};

const syncActiveTabFromLocation = () => {
  const tabName = window.location.hash.slice(1);
  setActiveTab(TAB_COPY[tabName] ? tabName : "overview", { updateHistory: false });
};

document.querySelectorAll("[data-tab]").forEach((tab) => {
  tab.addEventListener("click", () => setActiveTab(tab.dataset.tab));
});

document.querySelectorAll("[data-nav-target]").forEach((button) => {
  button.addEventListener("click", (event) => {
    if (button.tagName === "A") event.preventDefault();
    setActiveTab(button.dataset.navTarget);
    if (button.dataset.skillViewTarget) setSkillView(button.dataset.skillViewTarget);
  });
});

window.addEventListener("popstate", syncActiveTabFromLocation);
window.addEventListener("hashchange", syncActiveTabFromLocation);

document.querySelectorAll("[data-skill-view]").forEach((tab) => {
  tab.addEventListener("click", () => setSkillView(tab.dataset.skillView));
});

document.querySelectorAll("[data-lite-view]").forEach((tab) => {
  tab.addEventListener("click", () => setLiteView(tab.dataset.liteView));
});

document.querySelector("#compare-left-select").addEventListener("change", () => loadCompareVersions("left"));
document.querySelector("#compare-right-select").addEventListener("change", () => loadCompareVersions("right"));
document.querySelector("#compare-left-version-select").addEventListener("change", () => loadSkillContent("left"));
document.querySelector("#compare-right-version-select").addEventListener("change", () => loadSkillContent("right"));
document.querySelector("#skill-version-select").addEventListener("change", (event) => loadSkillVersion(event.currentTarget.value));
document.querySelector("#skill-preview-mode").addEventListener("click", () => {
  renderSkillContentPreview();
  setSkillContentMode("preview");
});
document.querySelector("#skill-edit-mode").addEventListener("click", () => setSkillContentMode("edit"));
document.querySelector("#skill-content-editor").addEventListener("input", () => {
  updateSkillEditorState();
  renderSkillContentPreview();
});
document.querySelector("#skill-version-label").addEventListener("input", updateSkillEditorState);
document.querySelector("#skill-commit-message").addEventListener("input", updateSkillEditorState);
document.querySelector("#publish-skill").addEventListener("click", publishSkillContent);
document.querySelector("#activate-skill-version").addEventListener("click", activateSelectedSkillVersion);
document.querySelector("#export-skill").addEventListener("click", exportSelectedSkillVersion);
document.querySelector("#sync-skill").addEventListener("click", syncSelectedSkill);
document.querySelector("#evolve-skill").addEventListener("click", evolveSelectedSkill);
document.querySelector("#promote-skill").addEventListener("click", promoteSelectedSkill);
document.querySelector("#register-skill").addEventListener("click", registerSkill);
document.querySelector("#load-demo").addEventListener("click", () => {
  compareRequestTokens.left += 1;
  compareRequestTokens.right += 1;
  compareContentState.left = DEMO_LEFT;
  compareContentState.right = DEMO_RIGHT;
  comparePaneMessages.left = "";
  comparePaneMessages.right = "";
  renderCompareContent();
});

document.querySelector("#save-settings").addEventListener("click", saveConfig);
document.querySelector("#memory-search").addEventListener("click", () => {
  const query = document.querySelector("#memory-query").value.trim();
  loadMemories(memoryRequestPath(Boolean(query)));
});
document.querySelector("#memory-refresh").addEventListener("click", () => loadMemories(memoryRequestPath(false)));
document.querySelector("#memory-query").addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    const query = event.currentTarget.value.trim();
    loadMemories(memoryRequestPath(Boolean(query)));
  }
});
document.querySelector("#lite-load-traces").addEventListener("click", loadLiteTraces);
document.querySelector("#lite-refresh-traces").addEventListener("click", loadLiteTraces);
document.querySelector("#lite-trace-directory").addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    loadLiteTraces();
  }
});

const initialTab = window.location.hash.slice(1);
setActiveTab(TAB_COPY[initialTab] ? initialTab : "overview", { updateHistory: false });
setSkillView("library");
setLiteView("traces");
Promise.all([loadConfig(), loadSkills()]);
