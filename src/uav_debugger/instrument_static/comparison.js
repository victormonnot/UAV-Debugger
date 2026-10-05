(() => {
  "use strict";
  const $ = id => document.getElementById(id);
  const roles = ["baseline", "blackout"];
  const label = role => role === "baseline" ? "Baseline" : role === "blackout" ? "Blackout" : "Pair";
  const pointLabel = point => point === "receiver" ? "Receiver" : "Relay input";
  const text = (id, value) => { $(id).textContent = String(value ?? "Unavailable"); };
  const excerpt = (value, omitted) => omitted ? value + " [" + omitted + " characters omitted]" : value;
  const state = {
    active: false, result: null, request: 0, controller: null, busy: false,
    reportRequest: 0, reportController: null, reportBusy: false,
    baseline: { input: null, run: null, request: 0, controller: null, pending: false, error: "" },
    blackout: { input: null, run: null, request: 0, controller: null, pending: false, error: "" },
  };
  let actions, maxBytes = 64 * 1024 * 1024, maxFiles = 64;
  let plotQueue = Promise.resolve(), plotRevision = 0, plotView = 0;

  function seconds(value) {
    if (value == null) return null;
    const ns = BigInt(value), absolute = ns < 0n ? -ns : ns;
    return (ns < 0n ? "-" : "") + String(absolute / 1000000000n) + "." + String(absolute % 1000000000n).padStart(9, "0");
  }
  function rows(id, entries, values, decorate) {
    const fragment = document.createDocumentFragment();
    for (const item of entries) {
      const row = document.createElement("tr");
      for (const value of values(item)) {
        const cell = document.createElement("td"); cell.textContent = String(value ?? "Unavailable"); row.append(cell);
      }
      decorate?.(row, item); fragment.append(row);
    }
    $(id).replaceChildren(fragment);
  }
  function omission(id, count, noun) {
    text(id, count + " further " + noun + " omitted from this view"); $(id).hidden = !count;
  }
  function hasPair() { return roles.every(role => state[role].run); }
  function cancelReport() {
    state.reportRequest++; state.reportController?.abort(); state.reportController = null; state.reportBusy = false;
    $("comparison-report-error").hidden = true;
  }
  function cancelRequest() {
    state.request++; state.controller?.abort(); state.controller = null; state.busy = false; cancelReport();
  }
  function clearResult() {
    cancelRequest(); state.result = null; plotView++;
    $("comparison-error").hidden = true; renderResult(true);
  }
  function clearRole(role) {
    clearResult();
    const item = state[role];
    item.request++; item.controller?.abort();
    Object.assign(item, { input: null, run: null, controller: null, pending: false, error: "" });
    $("comparison-" + role + "-files").value = "";
    renderRoles();
  }
  function clear() { roles.forEach(clearRole); }
  function applied() {
    const selection = state.result?.selection;
    if (!selection) return {};
    return {
      source: selection.source.join(":"), message_type: selection.message_type,
      start: selection.start_s, ...(selection.end_s == null ? {} : { end: selection.end_s }),
    };
  }
  function draft() {
    return {
      source: $("comparison-source").value, message_type: $("comparison-message").value,
      start: $("comparison-start").value,
      ...(state.result?.selection.end_s == null ? {} : { end: $("comparison-end").value }),
    };
  }
  function updateDraft() {
    const before = applied(), after = draft();
    const pending = state.result && Object.keys(before).some(key => before[key] !== after[key]);
    text("comparison-filter-state", !state.result ? "No pair" : pending ? "Unapplied changes" : "Applied");
    $("comparison-filter-state").dataset.pending = String(Boolean(pending));
  }
  function controls() {
    const pending = state.busy || roles.some(role => state[role].pending);
    $("comparison-view").setAttribute("aria-busy", String(pending));
    $("comparison-fields").disabled = !state.result || roles.some(role => state[role].pending);
    $("comparison-point").disabled = !state.result?.comparable || roles.some(role => state[role].pending);
    $("comparison-download-report").disabled = !state.result || pending || state.reportBusy;
    $("comparison-download-report").setAttribute("aria-busy", String(state.reportBusy));
    $("comparison-clear").disabled = !roles.some(role => state[role].run || state[role].pending || state[role].error);
    $("comparison-reset-chart").disabled = !state.result?.activity.figure;
    $("comparison-retry").hidden = Boolean(state.result) || !hasPair() || $("comparison-error").hidden;
    $("comparison-retry").disabled = pending;
    for (const role of roles) {
      $("comparison-clear-" + role).disabled = !state[role].run && !state[role].pending && !state[role].error;
      $("comparison-inspect-" + role).disabled = !state[role].run || pending;
    }
    for (const id of ["catalog-compare", "compare-saved-runs"]) $(id).disabled = !hasPair() || pending;
    updateDraft(); actions?.changed?.();
  }
  function renderRoles() {
    for (const role of roles) {
      const item = state[role], run = item.run, prefix = "comparison-" + role + "-";
      text(prefix + "name", item.pending ? "Reading saved evidence..." : excerpt(run?.source_name, run?.source_name_omitted_characters) || "No " + role + " selected");
      text(prefix + "identity", run?.identity);
      text(prefix + "outcome", excerpt(run?.declared_outcome, run?.declared_outcome_omitted_characters)); text(prefix + "evidence", run?.evidence_status);
      text(prefix + "origin", run?.measurement_monotonic_ns);
      text(prefix + "requested", run?.requested_json || ""); text(prefix + "provenance", run?.provenance_json || "");
      $(prefix + "details").hidden = !run;
      text(prefix + "error", item.error); $(prefix + "error").hidden = !item.error;
      rows(prefix + "files-rows", run?.files || [], file => [file.name, file.size_bytes, file.sha256]);
      rows(prefix + "issue-rows", run?.issues.rows || [], issue => [issue.file_name, issue.code, excerpt(issue.message, issue.message_omitted_characters)]);
      omission(prefix + "issues-omitted", run?.issues.omitted_count ?? Math.max(0, (run?.issues.total_count || 0) - (run?.issues.rows.length || 0)), "reader issues");
      text(prefix + "ignored", run?.ignored_files?.join("\n") || "None");
      text("catalog-" + role + "-name", item.pending ? "Reading saved evidence..." : run?.source_name || "No " + role + " selected");
    }
    const errors = roles.filter(role => state[role].error).map(role => label(role) + ": " + state[role].error);
    text("catalog-pair-status", errors.join(" / ") || (roles.some(role => state[role].pending) ? "Validating selected evidence..." : ""));
    text("run-comparison-assigned", roles.filter(role => state[role].run).map(role => label(role) + ": " + state[role].run.source_name).join(" / "));
    controls();
  }
  function setOptions(id, values, selected) {
    $(id).replaceChildren(...values.map(([value, name]) => {
      const option = document.createElement("option"); option.value = value; option.textContent = name; return option;
    }));
    $(id).value = selected;
  }
  function messageOptions(selected) {
    const source = $("comparison-source").value;
    const options = state.result?.available_selections.find(item => item.source.join(":") === source)?.message_types || [];
    const types = options.length ? options : [selected || "ATTITUDE"];
    setOptions("comparison-message", types.map(value => [value, value]), types.includes(selected) ? selected : types.includes("ATTITUDE") ? "ATTITUDE" : types[0]);
  }
  function renderSelection() {
    const selection = state.result?.selection;
    const sources = state.result?.available_selections.map(item => [item.source.join(":"), item.source.join(" / ")]) || [];
    if (selection && !sources.some(([value]) => value === selection.source.join(":"))) sources.push([selection.source.join(":"), selection.source.join(" / ")]);
    setOptions("comparison-source", sources, selection?.source.join(":") || "");
    messageOptions(selection?.message_type);
    $("comparison-start").value = selection?.start_s || "0"; $("comparison-end").value = selection?.end_s || "";
    $("comparison-start").disabled = $("comparison-end").disabled = !selection || selection.end_s == null;
  }
  function renderResult(resetDraft = false) {
    const result = state.result, selection = result?.selection;
    text("comparison-status", result ? result.comparable ? "Comparison available" : "Comparison unavailable" : "Select a baseline and a blackout");
    $("comparison-status").dataset.state = result ? result.comparable ? "ready" : "warning" : "";
    text("comparison-window", selection ? "[" + selection.start_s + ", " + (selection.end_s ?? "Unavailable") + ") s" : "Unavailable");
    text("comparison-eligibility", result ? result.comparable ? "Eligible evidence / both points" : "Aggregate metrics unavailable" : "Not evaluated");
    rows("comparison-issue-rows", result?.reasons.rows || [], issue => [label(issue.run_role), issue.code + ": " + excerpt(issue.message, issue.message_omitted_characters)]);
    omission("comparison-reasons-omitted", result?.reasons.omitted_count || 0, "blocking reasons");
    rows("comparison-difference-rows", result?.differences.rows || [], item => [excerpt(item.field, item.field_omitted_characters), item.baseline_json, item.blackout_json, item.blocking ? "Yes" : "No"]);
    text("comparison-difference-count", result?.differences.total_count || "");
    text("comparison-differences-empty", result ? "No relevant configuration differences recorded" : "Not evaluated");
    $("comparison-differences-empty").hidden = Boolean(result?.differences.total_count);
    omission("comparison-differences-omitted", result?.differences.omitted_count || 0, "configuration differences");
    const gates = roles.flatMap(role => (result?.gates[role].rows || []).map(gate => ({ role, ...gate })));
    rows("comparison-gate-rows", gates, gate => [label(gate.role), gate.start_s, gate.end_s, gate.duration_s, "actions.jsonl:" + gate.start_line, gate.end_line == null ? null : "actions.jsonl:" + gate.end_line]);
    $("comparison-gates-empty").hidden = gates.length > 0;
    omission("comparison-gates-omitted", roles.reduce((count, role) => count + (result?.gates[role].omitted_count || 0), 0), "gate intervals");
    const metrics = result?.metrics.available ? result.metrics.points.flatMap(point => roles.map(role => ({ role, point: point.point, ...point[role] }))) : [];
    rows("comparison-metrics-rows", metrics, metric => [label(metric.role), pointLabel(metric.point), metric.count, metric.rate_hz, seconds(metric.longest_interval?.duration_ns)], (row, metric) => { row.dataset.role = metric.role; row.dataset.point = metric.point; });
    rows("comparison-delta-rows", result?.metrics.available ? result.metrics.points : [], point => [pointLabel(point.point), point.delta.count, point.delta.rate_hz, seconds(point.delta.longest_interval_ns)], (row, point) => { row.dataset.point = point.point; });
    $("comparison-metrics").hidden = !result?.metrics.available;
    $("comparison-metrics-empty").hidden = Boolean(result?.metrics.available);
    const references = [];
    for (const metric of metrics) {
      const seen = new Map();
      for (const [use, reference] of [["First", metric.first], ["Last", metric.last], ["Interval start", metric.longest_interval?.previous], ["Interval end", metric.longest_interval?.current]]) {
        if (!reference) continue;
        const key = reference.record_index;
        if (seen.has(key)) seen.get(key).use.push(use);
        else { const entry = { role: metric.role, point: metric.point, reference, use: [use] }; seen.set(key, entry); references.push(entry); }
      }
    }
    rows("comparison-references-rows", references, item => [label(item.role), pointLabel(item.point), item.reference.file_name + ":" + item.reference.line_number, item.reference.record_index, seconds(item.reference.measurement_ns), item.reference.monotonic_ns, item.reference.capture_timestamp_us, item.reference.offset, item.use.join(", ")]);
    $("comparison-point").value = selection?.point || "receiver";
    if (resetDraft) renderSelection();
    controls(); renderPlot();
  }
  function runRequest(input, signal) {
    const options = { cache: "no-store", credentials: "same-origin", signal };
    if (input.kind === "catalog") return { url: "/api/catalog/open?" + new URLSearchParams({ key: input.key }), options };
    const body = new FormData();
    for (const file of input.files) body.append("files", file, file.webkitRelativePath || file.name);
    return { url: "/api/run", options: { ...options, method: "POST", body } };
  }
  async function assign(role, input, run = null) {
    const finishFocus = InstrumentUI.retainFocus();
    clearRole(role);
    const item = state[role], request = ++item.request, controller = new AbortController();
    item.controller = controller; item.pending = true; renderRoles();
    try {
      if (input.kind === "run-upload" && (input.files.length > maxFiles || input.files.reduce((sum, file) => sum + file.size, 0) > maxBytes)) {
        throw new Error("Each role accepts at most 64 files and 64 MiB in total.");
      }
      if (!run) {
        const call = runRequest(input, controller.signal), response = await fetch(call.url, call.options);
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || "The saved evidence could not be read.");
        if (data.schema_version !== 6 || !data.run) throw new Error("The local service returned unsupported saved evidence.");
        run = data.run;
      }
      if (request !== item.request) return;
      item.input = input; item.run = run; item.pending = false; item.controller = null;
      renderRoles();
      if (state.active && hasPair()) await compare({}, "initial");
    } catch (error) {
      if (request !== item.request || error.name === "AbortError") return;
      item.error = error instanceof TypeError ? "The local service could not be reached. Reopen the saved evidence." : error.message;
      item.pending = false; item.controller = null; renderRoles();
    } finally {
      finishFocus({ restore: request === item.request });
    }
  }
  function pairRequest(filters, point, signal, report = false) {
    const query = new URLSearchParams({ point, ...(report ? { format: "markdown" } : {}) }), body = new FormData();
    for (const [key, value] of Object.entries(filters)) if (value != null) query.set(key, value);
    for (const role of roles) {
      const item = state[role];
      query.set(role + "_sha256", item.run.identity);
      if (item.input.kind === "catalog") query.set(role + "_key", item.input.key);
      else for (const file of item.input.files) body.append(role, file, file.webkitRelativePath || file.name);
    }
    return { url: "/api/comparison?" + query, options: { method: "POST", body, cache: "no-store", credentials: "same-origin", signal } };
  }
  function invalidate(error) {
    const affected = roles.includes(error.run_role) ? [error.run_role] : roles.filter(role => state[role].input?.kind === "catalog");
    for (const role of affected.length ? affected : roles) {
      clearRole(role); state[role].error = error.message + " Reopen the saved evidence.";
    }
    renderRoles();
  }
  async function compare(filters, purpose = "apply", point = state.result?.selection.point || "receiver") {
    if (!hasPair()) return;
    const finishFocus = InstrumentUI.retainFocus();
    cancelRequest();
    const request = state.request, controller = new AbortController();
    state.controller = controller; state.busy = true; $("comparison-error").hidden = true;
    text("comparison-status", "Comparing saved evidence..."); controls();
    const call = pairRequest(filters, point, controller.signal);
    try {
      const response = await fetch(call.url, call.options), data = await response.json();
      if (!response.ok) throw Object.assign(new Error(data.error || "The comparison could not be read."), { status: response.status, run_role: data.run_role });
      if (data.schema_version !== 6 || !data.comparison) throw new Error("The local service returned an unsupported comparison.");
      if (request !== state.request) return;
      state.result = data.comparison;
      for (const role of roles) state[role].run = state.result.runs[role];
      if (purpose !== "refresh") plotView++;
      renderRoles(); renderResult(!["point", "refresh"].includes(purpose));
    } catch (error) {
      if (request !== state.request || error.name === "AbortError") return;
      if (error.status === 409 || (error instanceof TypeError && roles.some(role => state[role].input?.kind === "catalog"))) invalidate(error);
      else renderResult(false);
      text("comparison-error", error instanceof TypeError ? "The local service could not be reached. Retry the comparison." : error.message);
      $("comparison-error").hidden = false;
    } finally {
      if (request === state.request) { state.busy = false; state.controller = null; controls(); }
      finishFocus({ restore: request === state.request && state.active });
    }
  }
  async function download() {
    if (!state.result || state.busy || state.reportBusy) return;
    const finishFocus = InstrumentUI.retainFocus();
    const request = ++state.reportRequest, controller = new AbortController();
    const filename = "uav-debugger-comparison-" + state.baseline.run.identity.slice(0, 8) + "-" + state.blackout.run.identity.slice(0, 8) + ".md";
    state.reportController = controller; state.reportBusy = true; $("comparison-report-error").hidden = true; controls();
    const call = pairRequest(applied(), state.result.selection.point, controller.signal, true);
    try {
      const response = await fetch(call.url, call.options);
      if (!response.ok) {
        const data = await response.json(); throw Object.assign(new Error(data.error || "The comparison report could not be generated."), { status: response.status, run_role: data.run_role });
      }
      if (!response.headers.get("content-type")?.startsWith("text/markdown")) throw new Error("The local service returned an unsupported report.");
      const blob = await response.blob();
      if (request !== state.reportRequest) return;
      const objectURL = URL.createObjectURL(blob), link = document.createElement("a");
      link.href = objectURL; link.download = filename; document.body.append(link); link.click(); link.remove();
      setTimeout(() => URL.revokeObjectURL(objectURL), 1000);
    } catch (error) {
      if (request !== state.reportRequest || error.name === "AbortError") return;
      if (error.status === 409 || (error instanceof TypeError && roles.some(role => state[role].input?.kind === "catalog"))) invalidate(error);
      text("comparison-report-error", error instanceof TypeError ? "The local service could not be reached. Retry the report." : error.message);
      $("comparison-report-error").hidden = false;
    } finally {
      if (request === state.reportRequest) { state.reportBusy = false; state.reportController = null; controls(); }
      finishFocus({ restore: request === state.reportRequest && state.active });
    }
  }
  function renderPlot() {
    const revision = ++plotRevision;
    plotQueue = plotQueue.catch(() => {}).then(async () => {
      if (revision !== plotRevision) return;
      const plot = $("comparison-plot"), figure = state.result?.activity.figure;
      if (!state.active) return;
      plot.hidden = !figure; $("comparison-plot-empty").hidden = Boolean(figure);
      text("comparison-plot-empty", state.result ? "Comparison unavailable / review eligibility" : "No comparison available");
      if (!figure) { globalThis.Plotly?.purge(plot); return; }
      const styles = getComputedStyle(document.documentElement), color = name => styles.getPropertyValue(name).trim();
      const data = structuredClone(figure.data), layout = structuredClone(figure.layout);
      data.forEach((trace, index) => {
        const value = color(index === 0 ? "--activity" : "--yaw");
        trace.line = { ...trace.line, color: value }; trace.marker = { ...trace.marker, color: value, opacity: 1 };
        trace.opacity = 1;
        trace.hoverlabel = { bgcolor: color("--field"), bordercolor: color("--control-line"), font: { color: color("--ink"), family: "IBM Plex Sans", size: 12 } };
      });
      layout.height = 360; layout.autosize = true; delete layout.width; delete layout.title;
      layout.margin = { l: 52, r: 16, t: 42, b: 62 };
      layout.paper_bgcolor = layout.plot_bgcolor = color("--page");
      layout.font = { family: "IBM Plex Sans, sans-serif", size: 12, color: color("--ink") };
      layout.legend = { orientation: "h", x: 0, y: 1.17 };
      layout.uirevision = state.baseline.run.identity + ":" + state.blackout.run.identity + ":" + plotView;
      for (const name of ["xaxis", "yaxis"]) {
        const axis = layout[name] || {};
        axis.gridcolor = color("--grid"); axis.zerolinecolor = color("--control-line");
        axis.tickfont = { color: color("--muted"), size: 11 };
        axis.title = { text: name === "xaxis" ? "Measurement elapsed (s)" : "Observations / bin", font: { color: color("--ink"), size: 12 } };
        axis.fixedrange = matchMedia("(pointer: coarse)").matches; layout[name] = axis;
      }
      await Plotly.react(plot, data, layout, { displayModeBar: false, displaylogo: false, responsive: false, scrollZoom: false });
    }).catch(() => {
      if (revision !== plotRevision || !state.active) return;
      $("comparison-plot").hidden = true; $("comparison-plot-empty").hidden = false;
      text("comparison-plot-empty", "Comparison chart could not be displayed");
    });
  }
  function enter() {
    state.active = true; renderRoles(); renderResult(false);
    if (hasPair()) compare(state.result ? applied() : {}, state.result ? "refresh" : "initial");
  }
  function leave() {
    state.active = false; cancelRequest(); plotRevision++;
    for (const role of roles) if (state[role].pending) {
      const item = state[role]; item.request++; item.controller?.abort(); item.controller = null; item.pending = false;
    }
    renderRoles();
  }
  function init(callbacks) {
    actions = callbacks;
    for (const role of roles) {
      const section = document.createElement("section"); section.className = "comparison-role"; section.setAttribute("aria-label", label(role) + " evidence");
      section.innerHTML = '<div class="comparison-role-heading"><h3>' + label(role) + '</h3><div class="comparison-role-actions"><input id="comparison-' + role + '-files" type="file" webkitdirectory multiple hidden><button id="comparison-open-' + role + '" class="icon-button bordered" aria-label="Open ' + role + ' experiment" data-tip="Open ' + role + '"><i data-lucide="folder-open" aria-hidden="true"></i></button><button id="comparison-inspect-' + role + '" class="icon-button bordered" aria-label="Inspect ' + role + ' in Saved experiment" data-tip="Inspect ' + role + '" disabled><i data-lucide="scan-search" aria-hidden="true"></i></button><button id="comparison-clear-' + role + '" class="icon-button bordered" aria-label="Clear ' + role + '" data-tip="Clear ' + role + '" disabled><i data-lucide="x" aria-hidden="true"></i></button></div></div><p id="comparison-' + role + '-name" class="comparison-role-name"></p><p id="comparison-' + role + '-error" class="field-error" role="alert" hidden></p><details id="comparison-' + role + '-details" hidden><summary>Settings and evidence</summary><dl class="stacked-facts"><dt>Run fingerprint / SHA-256</dt><dd id="comparison-' + role + '-identity"></dd><dt>Declared outcome</dt><dd id="comparison-' + role + '-outcome"></dd><dt>Evidence status</dt><dd id="comparison-' + role + '-evidence"></dd><dt>Measurement origin / host monotonic ns</dt><dd id="comparison-' + role + '-origin"></dd></dl><h4>Requested settings</h4><pre id="comparison-' + role + '-requested" class="evidence-code" tabindex="0" aria-label="' + label(role) + ' requested settings"></pre><h4>Provenance and clocks</h4><pre id="comparison-' + role + '-provenance" class="evidence-code" tabindex="0" aria-label="' + label(role) + ' provenance"></pre><div class="table-scroll" tabindex="0" role="region" aria-label="' + label(role) + ' evidence files"><table><thead><tr><th>File</th><th>Bytes</th><th>SHA-256</th></tr></thead><tbody id="comparison-' + role + '-files-rows"></tbody></table></div><div class="table-scroll" tabindex="0" role="region" aria-label="' + label(role) + ' reader issues"><table><thead><tr><th>File</th><th>Issue</th><th>Detail</th></tr></thead><tbody id="comparison-' + role + '-issue-rows"></tbody></table></div><p id="comparison-' + role + '-issues-omitted" class="microcopy" hidden></p><h4>Files excluded from analysis</h4><pre id="comparison-' + role + '-ignored" class="evidence-code"></pre></details>';
      $("comparison-roles").append(section);
      $("comparison-open-" + role).addEventListener("click", () => $("comparison-" + role + "-files").click());
      $("comparison-" + role + "-files").addEventListener("change", event => { const files = [...event.target.files]; if (files.length) assign(role, { kind: "run-upload", files }); });
      $("comparison-clear-" + role).addEventListener("click", () => clearRole(role));
      $("comparison-inspect-" + role).addEventListener("click", () => { if (state[role].input) actions.inspect(state[role].input); });
    }
    $("comparison-clear").addEventListener("click", clear);
    $("comparison-form").addEventListener("submit", event => { event.preventDefault(); if (state.result) compare(draft()); });
    $("comparison-retry").addEventListener("click", () => compare({}, "initial"));
    $("comparison-reset").addEventListener("click", () => compare({}, "initial"));
    $("comparison-source").addEventListener("change", () => { messageOptions($("comparison-message").value); updateDraft(); });
    for (const id of ["comparison-message", "comparison-start", "comparison-end"]) $(id).addEventListener("input", updateDraft);
    $("comparison-point").addEventListener("change", () => compare(applied(), "point", $("comparison-point").value));
    $("comparison-download-report").addEventListener("click", download);
    $("comparison-reset-chart").addEventListener("click", () => { plotView++; renderPlot(); });
    for (const id of ["catalog-compare", "compare-saved-runs"]) $(id).addEventListener("click", actions.show);
    let timer;
    new ResizeObserver(() => { clearTimeout(timer); timer = setTimeout(renderPlot, 80); }).observe($("comparison-plot"));
    document.fonts.ready.then(renderPlot);
    renderRoles(); renderResult(true);
  }
  globalThis.InstrumentComparison = { init, assign, enter, leave, renderPlot, hasPair,
    pair: () => Object.fromEntries(roles.map(role => [role, state[role].run?.source_name])),
    configure: config => { maxBytes = config.max_run_bytes || maxBytes; maxFiles = config.max_run_files || maxFiles; },
  };
})();
