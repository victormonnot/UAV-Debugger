(() => {
  "use strict";

  const $ = id => document.getElementById(id);
  const text = (id, value) => { $(id).textContent = String(value ?? "N/A"); };
  const runs = globalThis.InstrumentRuns;
  const comparison = globalThis.InstrumentComparison;
  const experiment = globalThis.InstrumentExperiment;
  let workspace = "analyze";
  const state = {
    data: null, input: null, retryInput: null, pendingInput: null,
    request: 0, controller: null, busy: false, busyPurpose: null, render: 0, viewRevision: 0,
    reportRequest: 0, reportController: null, reportBusy: false,
    view: "activity", inputMode: "recording", visible: [true, true, true], focused: false,
    observationRows: [], observationPage: 0,
  };
  const PAGE_SIZE = 50;
  let maxRecordingBytes = 10 * 1024 * 1024;
  let maxRunBytes = 64 * 1024 * 1024, maxRunFiles = 64;
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
    comparison.renderPlot();
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
    if (state.data.run) {
      const run = state.data.run;
      status(`Saved experiment / declared ${run.declared_outcome} / evidence ${run.evidence_status}`, run.evidence_status === "consistent" ? "ready" : "warning");
      return;
    }
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
    ["error", "filter-error", "gap-error", "issues-error", "messages-error", "inspector-error", "report-error", "run-error", "catalog-error"].forEach(id => { $(id).hidden = true; });
  }
  function resetPlotView() {
    state.viewRevision++;
    state.visible = [true, true, true];
    state.observationPage = 0;
    document.querySelectorAll("[data-series]").forEach(button => button.setAttribute("aria-pressed", "true"));
    $("observations-details").open = false;
  }
  function clearRecording() {
    runs.cancelCatalog();
    cancelReport();
    state.request++;
    state.controller?.abort();
    state.controller = null;
    state.input = state.retryInput = state.pendingInput = state.data = null;
    state.busy = false;
    state.busyPurpose = null;
    state.view = "activity";
    selectEvidence("evidence");
    $("recording-file").value = "";
    $("run-files").value = "";
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
      gap: state.data?.selection?.max_gap_s || "1",
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
    const hasRecords = Boolean(state.data?.recording?.record_count);
    const pending = hasRecords && ["source", "message_id", "start", "end"].some(key => draft[key] !== applied[key]);
    text("filter-state", !state.data ? "No input" : state.data.run && !state.data.recording ? "No capture" : !hasRecords ? "No records" : pending ? "Unapplied changes" : "Applied");
    $("filter-state").dataset.pending = String(pending);
  }
  function updateControls() {
    const available = Boolean(state.data);
    const blocked = selectionBusy(), messages = state.data?.selection?.messages;
    $("filter-fields").disabled = !state.data?.recording?.record_count || blocked;
    $("line-gap").disabled = $("apply-gap").disabled = !state.data?.recording?.record_count || blocked;
    $("clear-recording").disabled = !available && !state.pendingInput && !state.retryInput;
    $("analysis-workspace").setAttribute("aria-busy", String(state.busy));
    $("issues-previous").disabled = state.busy || !available || state.data.issue_page === 0;
    $("issues-next").disabled = state.busy || !available || (state.data.issue_page + 1) * state.data.issue_page_size >= state.data.issue_count;
    $("record-index").disabled = $("inspect-record").disabled = !messages?.total_count || blocked;
    $("messages-previous").disabled = state.busy || !messages || messages.page === 0;
    $("messages-next").disabled = state.busy || !messages || messages.page + 1 >= messages.page_count;
    $("messages-page").disabled = state.busy || !messages?.total_count;
    $("download-report").disabled = !available || state.busy || state.reportBusy;
    $("download-report").setAttribute("aria-busy", String(state.reportBusy));
    $("clear-inspector").disabled = blocked;
    document.querySelectorAll("#message-rows button").forEach(button => { button.disabled = blocked; });
    $("observation-point").disabled = !state.data?.run?.captures.some(capture => capture.present) || (blocked && state.busyPurpose !== "point");
    runs.setBusy(state.busy);
    for (const role of ["baseline", "blackout"]) $("use-run-" + role).disabled = !state.data?.run || state.busy;
    updateFilterState();
  }
  function selectionBusy() {
    return state.busy && ["import", "filters", "gap", "point"].includes(state.busyPurpose);
  }
  function runParameters() {
    const run = state.data?.run;
    return run ? {
      point: run.selected_point, run_sha256: run.identity, evidence_page: run.issues.page,
      trace: run.trace.kind, trace_page: run.trace.page, gate_page: run.gates.page,
    } : {};
  }
  function isRunInput(input) { return ["run-upload", "catalog"].includes(input?.kind); }
  function renderInputMode() {
    const mode = state.inputMode;
    for (const name of ["recording", "saved", "catalog", "comparison"]) {
      $(`${name}-input-tab`).setAttribute("aria-selected", String(mode === name));
      $(`${name}-input-tab`).tabIndex = mode === name ? 0 : -1;
    }
    $("catalog-view").hidden = mode !== "catalog";
    $("comparison-view").hidden = mode !== "comparison";
    $("status").hidden = mode === "comparison";
    for (const id of ["recording-header", "report-actions", "analysis-workspace"]) $(id).hidden = ["catalog", "comparison"].includes(mode);
    $("run-upload-actions").hidden = mode !== "saved";
    $("run-comparison-actions").hidden = mode !== "saved" || !state.data?.run;
    $("open-recording").hidden = $("load-example").hidden = mode !== "recording";
    $("clear-recording").setAttribute("aria-label", mode === "saved" ? "Clear experiment" : "Clear recording");
    $("clear-recording").dataset.tip = mode === "saved" ? "Clear experiment" : "Clear recording";
  }
  function selectInputMode(mode) {
    if (mode === state.inputMode) return;
    if (state.inputMode === "comparison") comparison.leave();
    clearRecording(); state.inputMode = mode; renderRecording(true);
    if (mode === "catalog") runs.loadCatalog();
    if (mode === "comparison") comparison.enter();
  }
  function invalidateLocalRun(message) {
    clearRecording(); state.inputMode = "catalog"; renderInputMode();
    text("catalog-error", `${message} Refresh the catalog and reopen the saved evidence.`);
    $("catalog-error").hidden = false;
    status("Saved evidence unavailable / previous view cleared", "error");
  }
  function requestOptions(input, filters, extra, signal) {
    const query = new URLSearchParams();
    for (const key of ["source", "message_id"]) { if (filters[key]) query.set(key, filters[key]); }
    for (const key of ["start", "end", "gap"]) {
      if (filters[key] !== undefined && filters[key] !== null) query.set(key, filters[key]);
    }
    for (const [key, value] of Object.entries(extra)) {
      if (value !== null && value !== undefined) query.set(key, String(value));
    }
    if (input.kind === "file") query.set("name", input.file.name);
    const options = { signal, cache: "no-store", credentials: "same-origin" };
    if (input.kind === "file") {
      options.method = "POST";
      options.headers = { "Content-Type": "application/octet-stream" };
      options.body = input.file;
    }
    if (input.kind === "run-upload") {
      const body = new FormData();
      for (const file of input.files) body.append("files", file, file.webkitRelativePath || file.name);
      options.method = "POST"; options.body = body;
    }
    if (input.kind === "catalog") query.set("key", input.key);
    const endpoint = { file: "/api/analyze", example: "/api/example", "run-upload": "/api/run", catalog: "/api/catalog/open" }[input.kind];
    return { url: `${endpoint}?${query}`, options };
  }
  async function openInput(input) {
    clearRecording();
    if (input.kind !== "catalog") state.inputMode = input.kind === "run-upload" ? "saved" : "recording";
    renderInputMode();
    if (input.kind === "file" && input.file.size > maxRecordingBytes) {
      text("error-message", "Recording exceeds the 10 MiB input limit.");
      $("error").hidden = false;
      $("retry-example").hidden = true;
      status("Input too large", "error");
      return;
    }
    if (input.kind === "run-upload" && (input.files.length > maxRunFiles || input.files.reduce((sum, file) => sum + file.size, 0) > maxRunBytes)) {
      text("error-message", "A saved experiment accepts at most 64 files and 64 MiB in total.");
      $("error").hidden = false; $("retry-example").hidden = true;
      status("Saved experiment exceeds input limits", "error"); return;
    }
    state.retryInput = input;
    await analyze(input, {}, "import");
  }
  async function analyze(input, filters, purpose, issuePage = 0, extra = {}) {
    cancelReport();
    const request = ++state.request;
    state.controller?.abort();
    const controller = new AbortController();
    state.controller = controller;
    state.pendingInput = input;
    state.busy = true;
    state.busyPurpose = purpose;
    hideErrors();
    updateControls();
    status(purpose === "import" ? "Reading input..." : purpose === "point" ? "Reading capture point..." : purpose === "run-page" ? "Reading saved evidence..." : purpose === "issues" ? "Reading import issues..." : purpose === "inspect" ? "Reading record..." : purpose === "messages" ? "Reading messages..." : "Applying selection...", "loading");
    const references = ["issues", "run-page"].includes(purpose) ? { record_page: state.data.selection?.messages.page, record_index: state.data.inspector?.index } : {};
    const { url, options } = requestOptions(input, filters, {
      issue_page: isRunInput(input) && !state.data?.recording ? null : issuePage,
      sha256: ["import", "point"].includes(purpose) ? null : state.data.recording?.sha256,
      ...(purpose === "import" ? {} : runParameters()),
      ...references, ...extra,
    }, controller.signal);
    try {
      const response = await fetch(url, options);
      let data;
      try { data = await response.json(); }
      catch { throw new Error("The local service returned an unreadable response."); }
      if (!response.ok) throw Object.assign(new Error(data.error || "The input could not be analyzed."), { status: response.status });
      if (data.schema_version !== 6 || (!data.run && !data.recording) || (data.recording && !data.selection?.messages) || !Array.isArray(data.sources) || !Array.isArray(data.message_types) || !Array.isArray(data.issues)) {
        throw new Error("The local service returned an unsupported recording response.");
      }
      if (request !== state.request) return;
      if (purpose === "issues") {
        for (const key of ["issues", "issue_count", "issue_counts", "issue_page", "issue_page_size"]) state.data[key] = data[key];
        renderIssues();
      } else if (purpose === "run-page") {
        state.data.run = data.run;
        runs.render(data.run);
      } else if (purpose === "inspect" || purpose === "messages") {
        state.data.selection.messages = data.selection.messages;
        state.data.inspector = data.inspector;
        renderMessages();
        renderInspector();
        if (purpose === "inspect") { exitFocus(); selectEvidence("inspector"); }
        renderPlot();
      } else {
        state.data = data;
        state.input = input;
        state.retryInput = null;
        if (purpose === "import" && data.run) { state.view = "run"; state.inputMode = "saved"; }
        resetPlotView();
        renderRecording(purpose === "import" || purpose === "point");
        if (purpose === "filters") setFilterDraft(appliedFilters());
        if (purpose === "import" || purpose === "gap") $("line-gap").value = data.selection?.max_gap_s || "1";
      }
      recordingStatus();
    } catch (error) {
      if (request !== state.request || error.name === "AbortError") return;
      const message = error instanceof TypeError ? "The local service could not be reached. Retry the request." : error.message;
      if (input.kind === "catalog" && (error.status === 409 || error instanceof TypeError)) {
        invalidateLocalRun(message);
      } else if (purpose === "point") {
        clearRecording(); state.retryInput = input;
        text("error-message", message); $("error").hidden = false; $("retry-example").hidden = false;
        status("Capture point unavailable / previous view cleared", "error"); updateControls();
      } else if (purpose === "import") {
        text("error-message", message);
        $("error").hidden = false;
        $("retry-example").hidden = false;
        status("Import failed", "error");
        if (input.kind === "catalog") { text("catalog-error", message); $("catalog-error").hidden = false; }
      } else {
        if (purpose === "run-page") runs.render(state.data.run);
        if (purpose === "messages") renderMessages();
        const target = ["point", "run-page"].includes(purpose) ? "run-error" : purpose === "gap" ? "gap-error" : purpose === "issues" ? "issues-error" : purpose === "inspect" ? "inspector-error" : purpose === "messages" ? "messages-error" : "filter-error";
        if (purpose === "point") selectView("run");
        if (purpose === "inspect") { exitFocus(); selectEvidence("inspector"); }
        text(target, message);
        $(target).hidden = false;
        status("Request not applied / previous selection retained", "error");
      }
    } finally {
      if (request === state.request) {
        state.busy = false;
        state.busyPurpose = null;
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
    const data = state.data, r = data?.recording, run = data?.run;
    text("input-caption", state.inputMode === "saved" ? "Experiment" : "Recording");
    text("recording-kind", run ? (state.input?.kind === "catalog" ? "Local saved experiment" : "Uploaded saved experiment") : r ? (r.synthetic ? "Synthetic example" : "Uploaded recording") : "No input");
    text("recording-name", run?.source_name || r?.source_name || (state.inputMode === "saved" ? "No saved experiment selected" : "No recording selected"));
    const wire = r?.wire_versions.length ? `MAVLink ${r.wire_versions.join(" + ")}` : "No accepted frames";
    text("recording-meta", run ? `${run.schema} / declared ${run.declared_outcome} / evidence ${run.evidence_status}` : r ? `${r.capture_span_s ?? "N/A"} s capture range / ${r.size_bytes} bytes / ${wire}` : state.inputMode === "saved" ? "Saved evidence / 64 MiB maximum" : "QGC timestamped MAVLink / 10 MiB maximum");
    for (const key of ["record", "decoded", "opaque", "source"]) text(`${key}-count`, r?.[`${key}_count`] ?? (run ? "N/A" : 0));
    const outcomes = { complete: "Complete import", stopped: "Partial import", empty: "Empty recording" };
    text("import-status", r ? outcomes[r.traversal] || r.traversal : "No import");
    text("import-description", run ? (r ? r.source_name : "No available capture") : r ? (r.synthetic ? "Bundled synthetic recording" : "Original uploaded bytes") : "Original recording");
    text("input-size", r ? `${r.size_bytes} B` : null);
    text("consumed-size", r ? `${r.consumed_bytes} B` : null);
    text("remaining-size", r ? `${r.remaining_bytes} B` : null);
    text("capture-span", r && r.capture_span_s !== null ? `${r.capture_span_s} s` : null);
    text("capture-origin", r?.capture_origin_us);
    for (const [id, key] of [["profile", "profile"], ["dialect", "dialect"], ["decoder", "decoder_version"], ["sha256", "sha256"]]) text(id, r?.[key]);
    text("footer-state", run ? "SAVED EVIDENCE / READ-ONLY" : r?.synthetic ? "SYNTHETIC EXAMPLE / FILE-ONLY" : "FILE-ONLY ANALYSIS");
    $("run-point-field").hidden = !run;
    text("capture-name", r?.source_name || "No available capture");
    if (run) {
      for (const option of $("observation-point").options) option.disabled = !run.captures.find(capture => capture.point === option.value)?.present;
      $("observation-point").value = run.selected_point || "";
    }
    runs.render(run || null, resetFilters && state.busyPurpose !== "point");
    renderInputMode();
    if (resetFilters) {
      setOptions("source-filter", "All sources", (data?.sources || []).map(source => [
        `${source.system_id}:${source.component_id}`, `${source.system_id} / ${source.component_id}`,
      ]));
      setOptions("message-filter", "All messages", (data?.message_types || []).map(message => [String(message.message_id), message.name]));
      setFilterDraft(appliedFilters());
      $("line-gap").value = data?.selection?.max_gap_s || "1";
      $("issues-details").open = false;
      $("provenance-details").open = false;
    }
    renderIssues();
    renderMessages();
    renderInspector();
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
  function renderMessages() {
    const messages = state.data?.selection?.messages, selected = state.data?.inspector?.index;
    const focusedIndex = document.activeElement?.closest("#message-rows button")?.dataset.recordIndex;
    const fragment = document.createDocumentFragment();
    for (const record of messages?.records || []) {
      const row = document.createElement("tr"), reference = document.createElement("td"), button = document.createElement("button");
      row.dataset.recordIndex = button.dataset.recordIndex = String(record.index);
      row.dataset.selected = String(selected === record.index);
      button.type = "button";
      button.textContent = `#${record.index}`;
      button.setAttribute("aria-label", `Inspect record #${record.index}`);
      button.setAttribute("aria-pressed", String(selected === record.index));
      button.addEventListener("click", () => inspectRecord(record.index));
      reference.append(button); row.append(reference);
      for (const value of [record.time_s, record.message_name || `ID ${record.message_id}`, `${record.system_id} / ${record.component_id}`, record.sequence, record.checksum_status]) {
        const cell = document.createElement("td"); cell.textContent = String(value); row.append(cell);
      }
      fragment.append(row);
    }
    $("message-rows").replaceChildren(fragment);
    if (focusedIndex !== undefined) {
      [...$("message-rows").querySelectorAll("button")].find(button => button.dataset.recordIndex === focusedIndex)?.focus({ preventScroll: true });
    }
    const count = messages?.total_count || 0, first = count ? messages.page * messages.page_size + 1 : 0;
    text("messages-range", messages ? `${first} to ${first ? first + messages.records.length - 1 : 0} of ${count} records` : "No input");
    text("messages-empty", state.data?.run && !state.data.recording ? "No available capture" : state.data ? "No records match the applied selection" : "No recording selected");
    $("messages-empty").hidden = count > 0;
    $("messages-page").value = String((messages?.page || 0) + 1);
    $("messages-page").max = String(messages?.page_count || 1);
    text("messages-page-count", messages?.page_count || 1);
  }
  function renderInspector() {
    const record = state.data?.inspector;
    $("inspector-content").hidden = !record;
    $("inspector-empty").hidden = Boolean(record);
    $("record-index").value = record ? String(record.index) : "";
    text("inspector-title", record ? `Record #${record.index}` : "Record");
    text("inspector-message", record ? `${record.message_name || "Opaque message"} / ${record.message_id}` : "");
    text("inspector-source", record ? `${record.system_id} / ${record.component_id}` : "");
    text("inspector-sequence", record?.sequence);
    text("inspector-wire", record ? `MAVLink ${record.wire_version}` : "");
    text("inspector-checksum", record?.checksum_status);
    text("inspector-capture", record?.timestamp_us);
    text("inspector-time", record?.time_s);
    text("inspector-record-range", record ? `[${record.offset}, ${record.end_offset})` : "");
    text("inspector-frame-range", record ? `[${record.frame_offset}, ${record.end_offset})` : "");
    // Keep server-rendered JSON as text: parsing it would round 64-bit payload clocks.
    text("inspector-fields", record?.fields_json || "");
    $("inspector-fields").hidden = !record?.fields_json;
    $("inspector-opaque").hidden = !record || record.fields_json !== null;
    text("inspector-raw-frame", record?.raw_frame_hex.match(/.{2}/g)?.join(" ") || "");
    text("inspector-raw-record", record?.raw_record_hex.match(/.{2}/g)?.join(" ") || "");
  }
  function inspectRecord(index) {
    if (!state.input || selectionBusy()) return;
    analyze(state.input, appliedFilters(), "inspect", state.data.issue_page, { record_index: index });
  }
  function changeMessagePage(page) {
    const messages = state.data?.selection?.messages;
    if (!state.input || state.busy || !Number.isSafeInteger(page) || page < 0 || page >= messages.page_count || page === messages.page) {
      $("messages-page").value = String((messages?.page || 0) + 1);
      return;
    }
    analyze(state.input, appliedFilters(), "messages", state.data.issue_page, { record_page: page });
  }
  function selectEvidence(view) {
    for (const item of ["evidence", "inspector"]) {
      $(`${item}-tab`).setAttribute("aria-selected", String(view === item));
      $(`${item}-tab`).tabIndex = view === item ? 0 : -1;
      $(`${item}-view`).hidden = view !== item;
    }
  }
  function cancelReport() {
    state.reportRequest++;
    state.reportController?.abort();
    state.reportController = null;
    state.reportBusy = false;
  }
  async function downloadReport() {
    if (!state.input || state.busy || state.reportBusy) return;
    const request = ++state.reportRequest, controller = new AbortController();
    const input = state.input, saved = Boolean(state.data.run);
    const fingerprint = state.data.run?.identity || state.data.recording.sha256;
    state.reportController = controller;
    state.reportBusy = true;
    $("report-error").hidden = true;
    updateControls();
    const { url, options } = requestOptions(state.input, appliedFilters(), {
      ...runParameters(), format: "markdown", sha256: state.data.recording?.sha256, record_index: state.data.inspector?.index,
    }, controller.signal);
    try {
      const response = await fetch(url, options);
      if (!response.ok) {
        const error = await response.json();
        throw Object.assign(new Error(error.error || "The report could not be generated."), { status: response.status });
      }
      if (!response.headers.get("content-type")?.startsWith("text/markdown")) throw new Error("The local service returned an unsupported report.");
      const blob = await response.blob();
      if (request !== state.reportRequest) return;
      const objectURL = URL.createObjectURL(blob), link = document.createElement("a");
      link.href = objectURL;
      link.download = `uav-debugger-${saved ? "run-" : ""}${fingerprint.slice(0, 12)}.md`;
      document.body.append(link); link.click(); link.remove();
      setTimeout(() => URL.revokeObjectURL(objectURL), 1000);
    } catch (error) {
      if (request !== state.reportRequest || error.name === "AbortError") return;
      if (input.kind === "catalog" && (error.status === 409 || error instanceof TypeError)) { invalidateLocalRun(error.message); return; }
      text("report-error", error instanceof TypeError ? "The local service could not be reached. Retry the report." : error.message);
      $("report-error").hidden = false;
    } finally {
      if (request === state.reportRequest) {
        state.reportBusy = false;
        state.reportController = null;
        updateControls();
      }
    }
  }
  function renderSelection() {
    const selection = state.data?.selection, attitude = selection?.attitude, summary = attitude?.summary;
    const isAttitude = state.view === "attitude", isMessages = state.view === "messages", isRun = state.view === "run", current = selection?.[state.view], ready = Boolean(current?.figure);
    const source = selection?.source ? `Source ${selection.source.join(" / ")}` : "All sources";
    const message = selection?.message_id === null ? "All messages" : state.data?.message_types.find(type => type.message_id === selection?.message_id)?.name || "All messages";
    text("selected-count", selection?.record_count ?? (state.data?.run ? "N/A" : 0));
    text("longest-interval", selection?.longest_interval_us != null ? `${secondsFromMicros(selection.longest_interval_us)} s` : "N/A");
    text("applied-source", selection ? source : "No input");
    text("applied-message", message);
    text("applied-range", selection?.start_s != null ? `${selection.start_s} to ${selection.end_s} s` : null);
    text("sample-count", summary?.record_count ?? 0);
    text("applied-gap", `Applied: ${selection?.max_gap_s || "1"} s`);
    text("chart-title", isAttitude ? "Attitude / rad" : "Message activity");
    text("plot-context", selection ? (isAttitude && summary?.source ? `Source ${summary.source.join(" / ")}` : source) : "No input");
    for (const view of ["run", "activity", "attitude", "messages"]) {
      $(`${view}-tab`).setAttribute("aria-selected", String(state.view === view));
      $(`${view}-tab`).tabIndex = state.view === view ? 0 : -1;
      if (["activity", "attitude"].includes(view)) $(`${view}-plot`).hidden = state.view !== view || !ready;
    }
    $("chart-view").hidden = isMessages || isRun;
    $("run-view").hidden = !isRun;
    $("messages-view").hidden = !isMessages;
    if (!isMessages && !isRun) $("chart-view").setAttribute("aria-labelledby", `${state.view}-tab`);
    document.querySelector(".chart-panel").setAttribute("aria-labelledby", `${state.view}-tab`);
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
    $("reset-chart").disabled = isRun ? !state.data?.run?.timeline.figure : !ready;
    $("focus-chart").disabled = !selection && !state.data?.run;
    $("observations-details").hidden = !ready;
    text("plot-summary", !selection ? "Capture time relative to the first recorded message."
      : isAttitude ? `${summary.plotted_record_count} plotted / ${summary.record_count} ATTITUDE observations / line gap ${selection.max_gap_s} s`
      : `${selection.record_count} selected records / ${current?.figure?.data?.[0]?.y.length ?? 0} time bins`);
    buildObservationTable();
    renderPlot();
  }
  function buildObservationTable() {
    const figure = state.data?.selection?.[state.view]?.figure, isAttitude = state.view === "attitude";
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
    runs.renderPlot();
    const revision = ++state.render;
    plotQueue = plotQueue.catch(() => {}).then(async () => {
      if (revision !== state.render) return;
      if (!state.data?.selection) {
        for (const view of ["activity", "attitude"]) globalThis.Plotly?.purge($(`${view}-plot`));
        return;
      }
      if (["run", "messages"].includes(state.view)) return;
      const figure = state.data.selection[state.view]?.figure, plot = $(`${state.view}-plot`);
      if (!figure) { globalThis.Plotly?.purge(plot); return; }
      if (!globalThis.Plotly) throw new Error("The local chart library is unavailable.");
      const styles = getComputedStyle(document.documentElement), color = name => styles.getPropertyValue(name).trim();
      const data = structuredClone(figure.data), layout = structuredClone(figure.layout);
      const colors = [color("--roll"), color("--pitch"), color("--yaw")];
      data.forEach((trace, index) => {
        if (state.view === "attitude") {
          trace.line = { ...trace.line, color: colors[index], width: 2 };
          trace.marker = { ...trace.marker, color: colors[index], opacity: 1, size: trace.customdata.map(reference => reference && reference[0] === state.data.inspector?.index ? 11 : 7) };
          trace.unselected = { marker: { opacity: 1 } };
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
      plot.removeAllListeners("plotly_click");
      if (state.view === "attitude" && revision === state.render) {
        const viewRevision = state.viewRevision;
        plot.on("plotly_click", event => {
          if (state.view !== "attitude" || state.viewRevision !== viewRevision || selectionBusy()) return;
          const point = event.points?.[0];
          if (!point || !Number.isInteger(point.curveNumber) || !Number.isInteger(point.pointNumber)) return;
          const trace = state.data?.selection.attitude.figure?.data[point.curveNumber];
          const reference = trace?.customdata?.[point.pointNumber];
          if (!reference || trace.y[point.pointNumber] === null || !Number.isSafeInteger(reference[0])) return;
          if (point.customdata?.[0] !== reference[0] || point.customdata?.[1] !== reference[1]) return;
          inspectRecord(reference[0]);
        });
      }
    }).catch(() => {
      if (revision !== state.render) return;
      if (["run", "messages"].includes(state.view)) return;
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
  function bindTabs(views, select) {
    for (const view of views) {
      $(`${view}-tab`).addEventListener("click", () => select(view));
      $(`${view}-tab`).addEventListener("keydown", event => {
        if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
        event.preventDefault();
        const available = views.filter(name => !$(`${name}-tab`).hidden), index = available.indexOf(view);
        const next = event.key === "Home" ? 0 : event.key === "End" ? available.length - 1 : (index + (event.key === "ArrowRight" ? 1 : -1) + available.length) % available.length;
        select(available[next]); $(`${available[next]}-tab`).focus();
      });
    }
  }
  runs.init({
    openCatalog: key => openInput({ kind: "catalog", key }),
    assign: (role, key) => comparison.assign(role, { kind: "catalog", key }),
    page: parameters => {
      if (state.input && !state.busy) analyze(state.input, appliedFilters(), "run-page", state.data.issue_page, parameters);
    },
  });
  experiment.init({
    inspect: key => { selectWorkspace("analyze"); selectInputMode("saved"); openInput({ kind: "catalog", key }); },
    assign: (role, key, run) => comparison.assign(role, { kind: "catalog", key }, run),
    compare: () => { selectWorkspace("analyze"); selectInputMode("comparison"); },
    hasPair: () => comparison.hasPair(),
    pair: () => comparison.pair(),
    browse: () => { selectWorkspace("analyze"); if (state.inputMode === "catalog") runs.loadCatalog(); else selectInputMode("catalog"); },
  });
  comparison.init({
    show: () => selectInputMode("comparison"),
    inspect: input => { selectInputMode("saved"); openInput(input); },
    changed: () => experiment.controls(),
  });
  for (const role of ["baseline", "blackout"]) $("use-run-" + role).addEventListener("click", () => {
    if (state.input && state.data?.run && !state.busy) comparison.assign(role, state.input, state.data.run);
  });
  bindTabs(["run", "activity", "attitude", "messages"], selectView);
  bindTabs(["recording-input", "saved-input", "catalog-input", "comparison-input"], value => selectInputMode(value.replace("-input", "")));
  bindTabs(["evidence", "inspector"], selectEvidence);
  bindTabs(["frame-bytes", "record-bytes"], view => {
    for (const kind of ["frame", "record"]) {
      const selected = view === `${kind}-bytes`;
      $(`${kind}-bytes-tab`).setAttribute("aria-selected", String(selected));
      $(`${kind}-bytes-tab`).tabIndex = selected ? 0 : -1;
      $(`inspector-raw-${kind}`).hidden = !selected;
    }
  });
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
    if (state.view === "run") { runs.resetChart()?.catch(() => {}); return; }
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
  $("open-run").addEventListener("click", () => $("run-files").click());
  $("run-files").addEventListener("change", event => {
    const files = [...event.target.files];
    if (files.length) openInput({ kind: "run-upload", files });
  });
  $("observation-point").addEventListener("change", () => {
    if (!state.input || !state.data?.run) return;
    const point = $("observation-point").value;
    if (!point || (point === state.data.run.selected_point && state.busyPurpose !== "point")) return;
    state.data = { ...state.data, recording: null, selection: null, inspector: null, sources: [], message_types: [], issues: [], issue_count: 0, issue_counts: {}, issue_page: 0 };
    resetPlotView(); renderRecording(true);
    $("observation-point").value = point;
    analyze(state.input, {}, "point", 0, { point });
  });
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
    if (state.input && !selectionBusy()) analyze(state.input, filterDraft(), "filters");
  });
  $("reset-filters").addEventListener("click", () => {
    if (!state.input || selectionBusy()) return;
    const recording = state.data.recording;
    setFilterDraft({ source: "", message_id: "", start: recording.start_s, end: recording.end_s });
    analyze(state.input, filterDraft(), "filters");
  });
  $("gap-form").addEventListener("submit", event => {
    event.preventDefault();
    if (state.input && !selectionBusy()) analyze(state.input, { ...appliedFilters(), gap: $("line-gap").value }, "gap");
  });
  $("record-form").addEventListener("submit", event => {
    event.preventDefault();
    if (!/^\d+$/.test($("record-index").value)) {
      text("inspector-error", "Record index must be a non-negative integer.");
      $("inspector-error").hidden = false;
      selectEvidence("inspector");
      return;
    }
    inspectRecord($("record-index").value);
  });
  $("messages-page").addEventListener("change", () => changeMessagePage(Number($("messages-page").value) - 1));
  $("messages-page").addEventListener("keydown", event => {
    if (event.key === "Enter") { event.preventDefault(); changeMessagePage(Number($("messages-page").value) - 1); }
  });
  $("clear-inspector").addEventListener("click", () => {
    if (!state.data || selectionBusy()) return;
    cancelReport();
    state.request++;
    state.controller?.abort();
    state.controller = state.pendingInput = state.busyPurpose = null;
    state.busy = false;
    state.data.inspector = null;
    $("inspector-error").hidden = true;
    renderInspector(); renderMessages(); renderPlot(); updateControls(); recordingStatus();
  });
  $("download-report").addEventListener("click", downloadReport);
  for (const [direction, step] of [["previous", -1], ["next", 1]]) {
    $(`issues-${direction}`).addEventListener("click", () => {
      if (state.input && !state.busy) analyze(state.input, appliedFilters(), "issues", state.data.issue_page + step);
    });
    $(`observations-${direction}`).addEventListener("click", () => { state.observationPage += step; renderObservationPage(); });
    $(`messages-${direction}`).addEventListener("click", () => changeMessagePage(state.data.selection.messages.page + step));
  }
  function selectWorkspace(mode) {
    if (workspace === mode) return;
    if (workspace === "experiment") experiment.leave();
    else { cancelReport(); if (state.inputMode === "comparison") comparison.leave(); }
    workspace = mode;
    $("analyze-view").hidden = mode !== "analyze";
    $("experiment-view").hidden = mode !== "experiment";
    $("experiment-footer").hidden = mode !== "experiment";
    $("footer-state").hidden = document.querySelector(".footer-provenance").hidden = mode === "experiment";
    for (const name of ["analyze", "experiment"]) {
      const button = $(name + "-button");
      button.classList.toggle("active", mode === name);
      if (mode === name) button.setAttribute("aria-current", "page");
      else button.removeAttribute("aria-current");
    }
    document.querySelector(".navigation-context").textContent = mode === "experiment" ? "Explicit local execution" : "Saved observations";
    if (mode === "experiment") experiment.enter();
    else { renderPlot(); if (state.inputMode === "comparison") comparison.enter(); }
  }
  function openWorkspace() {
    text("workspace-title", "Existing workspace");
    text("workspace-context", "Recordings, saved experiments and execution");
    text("workspace-availability", classicURL ? "Separate local service / no recording is transferred." : "No existing workspace linked.");
    $("classic-link").hidden = !classicURL;
    if (classicURL) $("classic-link").href = classicURL;
    $("workspace-dialog").showModal();
  }
  $("experiment-button").addEventListener("click", () => selectWorkspace("experiment"));
  $("analyze-button").addEventListener("click", event => { event.preventDefault(); selectWorkspace("analyze"); });
  $("full-workspace").addEventListener("click", openWorkspace);
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
    if (Number.isSafeInteger(config.max_run_bytes) && config.max_run_bytes > 0) maxRunBytes = config.max_run_bytes;
    if (Number.isSafeInteger(config.max_run_files) && config.max_run_files > 0) maxRunFiles = config.max_run_files;
    runs.configure(config);
    comparison.configure(config);
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
