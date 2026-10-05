(() => {
  "use strict";

  const $ = id => document.getElementById(id);
  const text = (id, value) => { $(id).textContent = String(value ?? "N/A"); };
  const state = {
    data: null, input: null, retryInput: null, pendingInput: null,
    request: 0, controller: null, busy: false, render: 0, viewRevision: 0,
    view: "activity", visible: [true, true, true], focused: false,
    observationRows: [], observationPage: 0,
  };
  const PAGE_SIZE = 50;
  let maxRecordingBytes = 10 * 1024 * 1024;
  let plotQueue = Promise.resolve();
  let classicURL = null;
  const media = matchMedia("(prefers-color-scheme: dark)");
  const themeSelect = $("theme-select");
  themeSelect.value = document.documentElement.dataset.appearance || "system";

  function applyTheme(persist = true) {
    const preference = themeSelect.value;
    document.documentElement.dataset.appearance = preference;
    document.documentElement.dataset.theme = preference === "system"
      ? (media.matches ? "dark" : "light") : preference;
    if (persist) {
      try { localStorage.setItem("uav-debugger.appearance", preference); }
      catch { /* Storage is optional. */ }
    }
    renderPlot();
  }
  themeSelect.addEventListener("change", () => applyTheme());
  media.addEventListener("change", () => { if (themeSelect.value === "system") applyTheme(false); });
  addEventListener("storage", event => {
    if (event.key !== "uav-debugger.appearance") return;
    themeSelect.value = ["light", "dark", "system"].includes(event.newValue) ? event.newValue : "system";
    applyTheme(false);
  });

  function secondsFromMicros(value) {
    const number = BigInt(value), absolute = number < 0n ? -number : number;
    const fraction = (absolute % 1000000n).toString().padStart(6, "0").replace(/0+$/, "");
    return `${number < 0n ? "-" : ""}${absolute / 1000000n}${fraction ? `.${fraction}` : ""}`;
  }
  function status(message, kind = "ready") {
    text("status-message", message);
    $("status").dataset.state = kind;
  }
  function recordingStatus() {
    if (!state.data) { status("Ready"); return; }
    const r = state.data.recording;
    const outcome = r.traversal === "stopped" ? "Partial import" : r.traversal === "empty" ? "Empty input" : "Import complete";
    status(`${outcome} / ${r.record_count} records / ${state.data.issue_count} import issues`, r.traversal === "complete" ? "ready" : "warning");
  }
  function exitFocus() {
    state.focused = false;
    document.body.classList.remove("is-focused");
    $("focus-chart").setAttribute("aria-pressed", "false");
  }
  function hideErrors() {
    ["error", "filter-error", "gap-error", "issues-error"].forEach(id => { $(id).hidden = true; });
  }
  function resetPlotView() {
    state.viewRevision++;
    state.visible = [true, true, true];
    state.observationPage = 0;
    document.querySelectorAll("[data-series]").forEach(button => button.setAttribute("aria-pressed", "true"));
    $("observations-details").open = false;
  }
  function clearRecording() {
    state.request++;
    state.controller?.abort();
    state.controller = null;
    state.input = state.retryInput = state.pendingInput = state.data = null;
    state.busy = false;
    state.view = "activity";
    $("recording-file").value = "";
    exitFocus();
    resetPlotView();
    hideErrors();
    renderRecording(true);
    status("Ready");
  }
  function appliedFilters() {
    const selection = state.data?.selection;
    if (!selection) return {};
    return {
      source: selection.source?.join(":") || "",
      message_id: selection.message_id === null ? "" : String(selection.message_id),
      start: selection.start_s, end: selection.end_s, gap: selection.max_gap_s,
    };
  }
  function filterDraft() {
    return {
      source: $("source-filter").value, message_id: $("message-filter").value,
      start: $("start-filter").value, end: $("end-filter").value,
      gap: state.data?.selection.max_gap_s || "1",
    };
  }
  function setFilterDraft(filters) {
    $("source-filter").value = filters.source || "";
    $("message-filter").value = filters.message_id ?? "";
    $("start-filter").value = filters.start ?? "0";
    $("end-filter").value = filters.end ?? "0";
    updateFilterState();
  }
  function updateFilterState() {
    const draft = filterDraft(), applied = appliedFilters();
    const hasRecords = Boolean(state.data?.recording.record_count);
    const pending = hasRecords && ["source", "message_id", "start", "end"].some(key => draft[key] !== applied[key]);
    text("filter-state", !state.data ? "No input" : !hasRecords ? "No records" : pending ? "Unapplied changes" : "Applied");
    $("filter-state").dataset.pending = String(pending);
  }
  function updateControls() {
    const available = Boolean(state.data);
    $("filter-fields").disabled = !state.data?.recording.record_count || state.busy;
    $("line-gap").disabled = $("apply-gap").disabled = !state.data?.recording.record_count || state.busy;
    $("clear-recording").disabled = !available && !state.pendingInput && !state.retryInput;
    $("analysis-workspace").setAttribute("aria-busy", String(state.busy));
    $("issues-previous").disabled = state.busy || !available || state.data.issue_page === 0;
    $("issues-next").disabled = state.busy || !available || (state.data.issue_page + 1) * state.data.issue_page_size >= state.data.issue_count;
    updateFilterState();
  }
  async function openInput(input) {
    clearRecording();
    if (input.kind === "file" && input.file.size > maxRecordingBytes) {
      text("error-message", "Recording exceeds the 10 MiB input limit.");
      $("error").hidden = false;
      $("retry-example").hidden = true;
      status("Input too large", "error");
      return;
    }
    state.retryInput = input;
    await analyze(input, {}, "import");
  }
  async function analyze(input, filters, purpose, issuePage = 0) {
    const request = ++state.request;
    state.controller?.abort();
    const controller = new AbortController();
    state.controller = controller;
    state.pendingInput = input;
    state.busy = true;
    hideErrors();
    updateControls();
    status(purpose === "import" ? "Reading recording..." : purpose === "issues" ? "Reading import issues..." : "Applying selection...", "loading");
    const query = new URLSearchParams();
    for (const key of ["source", "message_id"]) { if (filters[key]) query.set(key, filters[key]); }
    for (const key of ["start", "end", "gap"]) {
      if (filters[key] !== undefined && filters[key] !== null) query.set(key, filters[key]);
    }
    query.set("issue_page", String(issuePage));
    if (input.kind === "file") query.set("name", input.file.name);
    const options = { signal: controller.signal, cache: "no-store", credentials: "same-origin" };
    if (input.kind === "file") {
      options.method = "POST";
      options.headers = { "Content-Type": "application/octet-stream" };
      options.body = input.file;
    }
    try {
      const endpoint = input.kind === "file" ? "/api/analyze" : "/api/example";
      const response = await fetch(`${endpoint}?${query}`, options);
      let data;
      try { data = await response.json(); }
      catch { throw new Error("The local service returned an unreadable response."); }
      if (!response.ok) throw new Error(data.error || "The recording could not be analyzed.");
      if (data.schema_version !== 2 || !data.recording || !data.selection || !Array.isArray(data.sources) || !Array.isArray(data.message_types) || !Array.isArray(data.issues)) {
        throw new Error("The local service returned an unsupported recording response.");
      }
      if (request !== state.request) return;
      if (purpose === "issues") {
        for (const key of ["issues", "issue_count", "issue_counts", "issue_page", "issue_page_size"]) state.data[key] = data[key];
        renderIssues();
      } else {
        state.data = data;
        state.input = input;
        state.retryInput = null;
        resetPlotView();
        renderRecording(purpose === "import");
        if (purpose === "filters") setFilterDraft(appliedFilters());
        if (purpose === "import" || purpose === "gap") $("line-gap").value = data.selection.max_gap_s;
      }
      recordingStatus();
    } catch (error) {
      if (request !== state.request || error.name === "AbortError") return;
      const message = error instanceof TypeError ? "The local service could not be reached. Retry the request." : error.message;
      if (purpose === "import") {
        text("error-message", message);
        $("error").hidden = false;
        $("retry-example").hidden = false;
        status("Import failed", "error");
      } else {
        const target = purpose === "gap" ? "gap-error" : purpose === "issues" ? "issues-error" : "filter-error";
        text(target, message);
        $(target).hidden = false;
        status("Request not applied / previous selection retained", "error");
      }
    } finally {
      if (request === state.request) {
        state.busy = false;
        state.controller = null;
        state.pendingInput = null;
        updateControls();
      }
    }
  }
  function setOptions(id, firstLabel, entries) {
    $(id).replaceChildren(new Option(firstLabel, ""), ...entries.map(([value, label]) => new Option(label, value)));
  }
  function renderRecording(resetFilters = false) {
    const data = state.data, r = data?.recording;
    text("recording-kind", r ? (r.synthetic ? "Synthetic example" : "Uploaded recording") : "No input");
    text("recording-name", r?.source_name || "No recording selected");
    const wire = r?.wire_versions.length ? `MAVLink ${r.wire_versions.join(" + ")}` : "No accepted frames";
    text("recording-meta", r ? `${r.capture_span_s ?? "N/A"} s capture range / ${r.size_bytes} bytes / ${wire}` : "QGC timestamped MAVLink / 10 MiB maximum");
    for (const key of ["record", "decoded", "opaque", "source"]) text(`${key}-count`, r?.[`${key}_count`] ?? 0);
    const outcomes = { complete: "Complete import", stopped: "Partial import", empty: "Empty recording" };
    text("import-status", r ? outcomes[r.traversal] || r.traversal : "No import");
    text("import-description", r ? (r.synthetic ? "Bundled synthetic recording" : "Original uploaded bytes") : "Original recording");
    text("input-size", r ? `${r.size_bytes} B` : null);
    text("consumed-size", r ? `${r.consumed_bytes} B` : null);
    text("remaining-size", r ? `${r.remaining_bytes} B` : null);
    text("capture-span", r && r.capture_span_s !== null ? `${r.capture_span_s} s` : null);
    text("capture-origin", r?.capture_origin_us);
    for (const [id, key] of [["profile", "profile"], ["dialect", "dialect"], ["decoder", "decoder_version"], ["sha256", "sha256"]]) text(id, r?.[key]);
    text("footer-state", r?.synthetic ? "SYNTHETIC EXAMPLE / FILE-ONLY" : "FILE-ONLY ANALYSIS");
    if (resetFilters) {
      setOptions("source-filter", "All sources", (data?.sources || []).map(source => [
        `${source.system_id}:${source.component_id}`, `${source.system_id} / ${source.component_id}`,
      ]));
      setOptions("message-filter", "All messages", (data?.message_types || []).map(message => [String(message.message_id), message.name]));
      setFilterDraft(appliedFilters());
      $("line-gap").value = data?.selection.max_gap_s || "1";
      $("issues-details").open = false;
      $("provenance-details").open = false;
    }
    renderIssues();
    renderSelection();
    updateControls();
  }
  function renderIssues() {
    const data = state.data, issues = data?.issues || [];
    text("issue-count", data?.issue_count ?? 0);
    $("issue-list").replaceChildren();
    $("issue-counts").replaceChildren();
    $("issues-empty").hidden = Boolean(data?.issue_count);
    text("issues-empty", data ? "No issues reported" : "No import");
    for (const [code, count] of Object.entries(data?.issue_counts || {})) {
      const label = document.createElement("dt"), value = document.createElement("dd");
      label.textContent = code; value.textContent = String(count);
      $("issue-counts").append(label, value);
    }
    for (const issue of issues) {
      const item = document.createElement("li"), code = document.createElement("strong");
      const message = document.createElement("p"), reference = document.createElement("small");
      item.dataset.severity = issue.severity;
      code.textContent = issue.code;
      message.textContent = issue.message;
      reference.textContent = `${issue.record_index === null ? "Input" : `Record #${issue.record_index}`} / byte ${issue.offset}`;
      item.append(code, message, reference);
      $("issue-list").append(item);
    }
    const pages = data ? Math.max(1, Math.ceil(data.issue_count / data.issue_page_size)) : 1;
    $("issues-pagination").hidden = pages <= 1;
    text("issues-page", `${(data?.issue_page || 0) + 1} / ${pages}`);
  }
  function renderSelection() {
    const selection = state.data?.selection, attitude = selection?.attitude, summary = attitude?.summary;
    const isAttitude = state.view === "attitude", current = selection?.[state.view], ready = Boolean(current?.figure);
    const source = selection?.source ? `Source ${selection.source.join(" / ")}` : "All sources";
    const message = selection?.message_id === null ? "All messages" : state.data?.message_types.find(type => type.message_id === selection?.message_id)?.name || "All messages";
    text("selected-count", selection?.record_count ?? 0);
    text("longest-interval", selection?.longest_interval_us != null ? `${secondsFromMicros(selection.longest_interval_us)} s` : "N/A");
    text("applied-source", selection ? source : "No input");
    text("applied-message", message);
    text("applied-range", selection?.start_s != null ? `${selection.start_s} to ${selection.end_s} s` : null);
    text("sample-count", summary?.record_count ?? 0);
    text("applied-gap", `Applied: ${selection?.max_gap_s || "1"} s`);
    text("chart-title", isAttitude ? "Attitude / rad" : "Message activity");
    text("plot-context", selection ? (isAttitude && summary?.source ? `Source ${summary.source.join(" / ")}` : source) : "No input");
    for (const view of ["activity", "attitude"]) {
      $(`${view}-tab`).setAttribute("aria-selected", String(state.view === view));
      $(`${view}-tab`).tabIndex = state.view === view ? 0 : -1;
      $(`${view}-plot`).hidden = state.view !== view || !ready;
    }
    $("chart-view").setAttribute("aria-labelledby", `${state.view}-tab`);
    $("empty-plot").hidden = Boolean(selection);
    $("plot-unavailable").hidden = !selection || ready;
    const emptyStatus = {
      multiple_sources: ["Multiple attitude sources", "A single source is required for the attitude plot."],
      too_many: ["Attitude display limit reached", `${summary?.record_count ?? 0} observations / ${summary?.point_limit ?? 5000}-point limit. No points are decimated.`],
      empty: ["No attitude observations", "No ATTITUDE records in the applied selection."],
    };
    const empty = isAttitude ? (emptyStatus[summary?.status] || emptyStatus.empty) : ["No selected observations", selection?.record_count === 0 ? "No records match the applied selection." : ""];
    text("plot-unavailable-text", empty[0]);
    text("plot-unavailable-detail", empty[1]);
    $("plot-legend").hidden = !isAttitude;
    $("gap-form").hidden = !isAttitude;
    document.querySelectorAll("[data-series]").forEach(button => { button.disabled = !isAttitude || !ready; });
    $("reset-chart").disabled = !ready;
    $("focus-chart").disabled = !selection;
    $("observations-details").hidden = !ready;
    text("plot-summary", !selection ? "Capture time relative to the first recorded message."
      : isAttitude ? `${summary.plotted_record_count} plotted / ${summary.record_count} ATTITUDE observations / line gap ${selection.max_gap_s} s`
      : `${selection.record_count} selected records / ${current?.figure?.data?.[0]?.y.length ?? 0} time bins`);
    buildObservationTable();
    renderPlot();
  }
  function buildObservationTable() {
    const figure = state.data?.selection[state.view]?.figure, isAttitude = state.view === "attitude";
    let rows = [];
    if (isAttitude) {
      const records = new Map();
      for (const [field, trace] of (figure?.data || []).entries()) {
        trace.customdata?.forEach((reference, index) => {
          if (!reference) return;
          const row = records.get(reference[0]) || { index: reference[0], offset: reference[2], timestamp: reference[1], values: [null, null, null] };
          row.values[field] = trace.y[index]; records.set(reference[0], row);
        });
      }
      rows = [...records.values()].sort((left, right) => left.index - right.index)
        .map(row => [`#${row.index}`, row.offset, ...row.values.map(value => value === null ? "N/A" : String(value)), row.timestamp]);
    } else {
      const trace = figure?.data?.[0];
      rows = trace?.customdata?.map((bounds, index) => [bounds[0], bounds[1], trace.y[index]]) || [];
    }
    state.observationRows = rows;
    state.observationPage = Math.min(state.observationPage, Math.max(0, Math.ceil(rows.length / PAGE_SIZE) - 1));
    text("observations-label", isAttitude ? "Plotted observations" : "Activity bins");
    text("observations-count", rows.length);
    text("observations-caption", isAttitude ? "Original plotted samples, with exact capture timestamps" : "Activity bins with exact relative time bounds and observation counts");
    const headers = isAttitude ? ["Record", "Offset (s)", "Roll (rad)", "Pitch (rad)", "Yaw (rad)", "Capture timestamp (us)"] : ["Start (s)", "End (s)", "Observations"];
    const row = document.createElement("tr");
    for (const heading of headers) {
      const cell = document.createElement("th"); cell.scope = "col"; cell.textContent = heading; row.append(cell);
    }
    $("observation-head").replaceChildren(row);
    renderObservationPage();
  }
  function renderObservationPage() {
    const rows = state.observationRows, page = state.observationPage;
    const fragment = document.createDocumentFragment();
    for (const values of rows.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE)) {
      const row = document.createElement("tr");
      for (const value of values) {
        const cell = document.createElement("td"); cell.textContent = String(value); row.append(cell);
      }
      fragment.append(row);
    }
    $("observation-rows").replaceChildren(fragment);
    const pages = Math.max(1, Math.ceil(rows.length / PAGE_SIZE));
    $("observations-pagination").hidden = pages === 1;
    $("observations-previous").disabled = page === 0;
    $("observations-next").disabled = page + 1 >= pages;
    text("observations-page", `${page + 1} / ${pages}`);
  }
  function renderPlot() {
    const revision = ++state.render;
    plotQueue = plotQueue.catch(() => {}).then(async () => {
      if (revision !== state.render) return;
      if (!state.data) {
        for (const view of ["activity", "attitude"]) globalThis.Plotly?.purge($(`${view}-plot`));
        return;
      }
      const figure = state.data.selection[state.view]?.figure, plot = $(`${state.view}-plot`);
      if (!figure) { globalThis.Plotly?.purge(plot); return; }
      if (!globalThis.Plotly) throw new Error("The local chart library is unavailable.");
      const styles = getComputedStyle(document.documentElement), color = name => styles.getPropertyValue(name).trim();
      const data = structuredClone(figure.data), layout = structuredClone(figure.layout);
      const colors = [color("--roll"), color("--pitch"), color("--yaw")];
      data.forEach((trace, index) => {
        if (state.view === "attitude") {
          trace.line = { ...trace.line, color: colors[index], width: 2 };
          trace.marker = { ...trace.marker, color: colors[index], size: 7 };
          trace.visible = state.visible[index];
        } else trace.marker = { ...trace.marker, color: color("--activity") };
        trace.hoverlabel = { bgcolor: color("--field"), bordercolor: color("--control-line"), font: { color: color("--ink"), family: "IBM Plex Sans", size: 12 } };
      });
      layout.height = plot.clientHeight || 465;
      layout.autosize = true;
      delete layout.width;
      layout.margin = { l: 66, r: 24, t: 22, b: 64 };
      layout.font = { family: "IBM Plex Sans, sans-serif", size: 12, color: color("--ink") };
      layout.paper_bgcolor = layout.plot_bgcolor = color("--page");
      layout.showlegend = false;
      layout.uirevision = `${state.viewRevision}:${state.view}`;
      layout.clickmode = "event";
      for (const [name, axis] of Object.entries(layout)) {
        if (!/^[xy]axis\d*$/.test(name)) continue;
        axis.gridcolor = color("--grid");
        axis.zerolinecolor = color("--control-line");
        axis.tickfont = { color: color("--muted"), size: 12 };
        axis.title = { ...axis.title, font: { color: color("--ink"), size: 12 } };
        axis.fixedrange = matchMedia("(pointer: coarse)").matches;
      }
      await Plotly.react(plot, data, layout, { displayModeBar: false, displaylogo: false, responsive: false, scrollZoom: false, showLink: false, locale: "en" });
    }).catch(() => {
      if (revision !== state.render) return;
      $(`${state.view}-plot`).hidden = true;
      $("plot-unavailable").hidden = false;
      text("plot-unavailable-text", "Chart unavailable");
      text("plot-unavailable-detail", "The local chart could not be displayed.");
    });
  }
  function selectView(view) {
    state.view = view;
    state.observationPage = 0;
    $("observations-details").open = false;
    renderSelection();
  }
  for (const view of ["activity", "attitude"]) {
    $(`${view}-tab`).addEventListener("click", () => selectView(view));
    $(`${view}-tab`).addEventListener("keydown", event => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      const next = event.key === "Home" ? "activity" : event.key === "End" ? "attitude" : view === "activity" ? "attitude" : "activity";
      selectView(next); $(`${next}-tab`).focus();
    });
  }
  let resizeTimer;
  const resizeObserver = new ResizeObserver(() => { clearTimeout(resizeTimer); resizeTimer = setTimeout(renderPlot, 80); });
  for (const view of ["activity", "attitude"]) resizeObserver.observe($(`${view}-plot`));
  document.fonts.ready.then(renderPlot);
  document.querySelectorAll("[data-series]").forEach(button => button.addEventListener("click", () => {
    const index = Number(button.dataset.series);
    state.visible[index] = !state.visible[index];
    button.setAttribute("aria-pressed", String(state.visible[index]));
    renderPlot();
  }));
  $("reset-chart").addEventListener("click", () => {
    const view = state.view, revision = state.viewRevision;
    plotQueue = plotQueue.catch(() => {}).then(() => {
      const plot = $(`${view}-plot`);
      if (!state.data || revision !== state.viewRevision || !plot.data?.length) return;
      const axes = Object.keys(plot.layout).filter(name => /^[xy]axis\d*$/.test(name));
      return Plotly.relayout(plot, Object.fromEntries(axes.map(name => [`${name}.autorange`, true])));
    }).catch(() => {});
  });
  $("focus-chart").addEventListener("click", () => {
    state.focused = !state.focused;
    document.body.classList.toggle("is-focused", state.focused);
    $("focus-chart").setAttribute("aria-pressed", String(state.focused));
    renderPlot();
  });
  $("open-recording").addEventListener("click", () => $("recording-file").click());
  $("recording-file").addEventListener("change", event => {
    const file = event.target.files[0];
    if (file) openInput({ kind: "file", file });
  });
  $("load-example").addEventListener("click", () => openInput({ kind: "example" }));
  $("retry-example").addEventListener("click", () => { if (state.retryInput) openInput(state.retryInput); });
  $("clear-recording").addEventListener("click", clearRecording);
  $("filter-form").addEventListener("input", updateFilterState);
  $("filter-form").addEventListener("change", updateFilterState);
  $("filter-form").addEventListener("submit", event => {
    event.preventDefault();
    if (state.input && !state.busy) analyze(state.input, filterDraft(), "filters");
  });
  $("reset-filters").addEventListener("click", () => {
    if (!state.input || state.busy) return;
    const recording = state.data.recording;
    setFilterDraft({ source: "", message_id: "", start: recording.start_s, end: recording.end_s });
    analyze(state.input, filterDraft(), "filters");
  });
  $("gap-form").addEventListener("submit", event => {
    event.preventDefault();
    if (state.input && !state.busy) analyze(state.input, { ...appliedFilters(), gap: $("line-gap").value }, "gap");
  });
  for (const [direction, step] of [["previous", -1], ["next", 1]]) {
    $(`issues-${direction}`).addEventListener("click", () => {
      if (state.input && !state.busy) analyze(state.input, appliedFilters(), "issues", state.data.issue_page + step);
    });
    $(`observations-${direction}`).addEventListener("click", () => { state.observationPage += step; renderObservationPage(); });
  }
  function openWorkspace(experiment) {
    text("workspace-title", experiment ? "Experiment" : "Existing workspace");
    text("workspace-context", experiment ? "Execution is available in the existing workspace." : "Record inspection, reports and saved experiments");
    text("workspace-availability", classicURL ? "Separate local service / no recording is transferred." : "No existing workspace linked.");
    $("classic-link").hidden = !classicURL;
    if (classicURL) $("classic-link").href = classicURL;
    $("workspace-dialog").showModal();
  }
  $("experiment-button").addEventListener("click", () => openWorkspace(true));
  $("full-workspace").addEventListener("click", () => openWorkspace(false));
  $("close-workspace").addEventListener("click", () => $("workspace-dialog").close());
  $("workspace-dialog").addEventListener("click", event => {
    if (event.target !== $("workspace-dialog")) return;
    const bounds = event.target.getBoundingClientRect();
    if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) event.target.close();
  });
  fetch("/api/config", { cache: "no-store", credentials: "same-origin" }).then(response => {
    if (!response.ok) throw new Error();
    return response.json();
  }).then(config => {
    text("version", `v${config.version}`);
    if (Number.isSafeInteger(config.max_recording_bytes) && config.max_recording_bytes > 0) maxRecordingBytes = config.max_recording_bytes;
    if (config.classic_url) {
      const url = new URL(config.classic_url);
      if (url.protocol === "http:" && ["127.0.0.1", "localhost", "[::1]"].includes(url.hostname)) { url.hostname = location.hostname; classicURL = url.href; }
    }
  }).catch(() => text("version", "Service unavailable"));
  const tooltip = $("tooltip");
  function hideTooltip() { tooltip.hidden = true; }
  function showTooltip(button) {
    if (button.disabled) return;
    tooltip.textContent = button.dataset.tip;
    tooltip.hidden = false;
    const bounds = button.getBoundingClientRect(), tip = tooltip.getBoundingClientRect();
    tooltip.style.left = Math.max(8, Math.min(innerWidth - tip.width - 8, bounds.left + bounds.width / 2 - tip.width / 2)) + "px";
    tooltip.style.top = (bounds.bottom + 8 + tip.height < innerHeight ? bounds.bottom + 8 : bounds.top - tip.height - 8) + "px";
  }
  document.querySelectorAll("[data-tip]").forEach(button => {
    button.addEventListener("mouseenter", () => showTooltip(button));
    button.addEventListener("focus", () => showTooltip(button));
    button.addEventListener("mouseleave", hideTooltip);
    button.addEventListener("blur", hideTooltip);
    button.addEventListener("click", hideTooltip);
  });
  addEventListener("scroll", hideTooltip, true);
  addEventListener("keydown", event => { if (event.key === "Escape") hideTooltip(); });
  if (globalThis.lucide) lucide.createIcons();
  renderRecording(true);
  if (new URLSearchParams(location.search).get("example") === "1") openInput({ kind: "example" });
})();
