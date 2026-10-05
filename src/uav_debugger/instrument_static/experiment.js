(() => {
  "use strict";

  const $ = id => document.getElementById(id);
  const text = (id, value) => { $(id).textContent = String(value ?? "N/A"); };
  const state = { visible: false, connected: false, busy: false, info: null,
    selected: null, followLatest: true, poll: 0, pollController: null, timer: null,
    handoff: 0, handoffController: null, handoffBusy: false, validated: null };
  let actions;
  const selected = () => state.info?.history.find(run => run.run_id === state.selected);

  function error(message) {
    text("experiment-error", message || "");
    $("experiment-error").hidden = !message;
  }
  function controls() {
    const run = selected(), info = state.info;
    const unavailable = !state.connected || state.busy;
    const locked = unavailable || Boolean(info?.active_run_id);
    const sitl = $("experiment-source").value === "arducopter-sitl";
    const blackout = $("experiment-scenario").value === "blackout";
    $("experiment-fields").disabled = locked;
    $("experiment-start").disabled = locked || (sitl && !info?.configuration.sitl_configured);
    $("experiment-stop").disabled = unavailable || !run?.active || run.state === "stopping";
    $("experiment-stop").setAttribute("aria-label", "Stop experiment");
    $("experiment-blackout-at").disabled = $("experiment-blackout-duration").disabled = !blackout;
    $("experiment-startup-field").hidden = !sitl;
    $("experiment-startup-timeout").disabled = !sitl;
    $("experiment-refresh").disabled = state.busy;
    $("experiment-history").disabled = !info?.history.length;
    for (const id of ["experiment-open-analyze", "experiment-use-baseline", "experiment-use-blackout"]) {
      $(id).disabled = unavailable || state.handoffBusy || !run?.can_open;
    }
    $("experiment-compare").disabled = state.handoffBusy || !actions?.hasPair();
    $("experiment-view").setAttribute("aria-busy", String(state.busy || state.handoffBusy));
    text("experiment-source-status", sitl
      ? (info?.configuration.sitl_configured ? "Pinned executable / isolated loopback" : "SITL is not configured on this server.")
      : "20 Hz ATTITUDE / UDP loopback");
    text("experiment-profile", sitl ? "ArduCopter SITL / disarmed / no vehicle commands" : "Synthetic MAVLink 2 / source 1:1 / common dialect");
    $("experiment-binary").hidden = !sitl || !info?.configuration.sitl_binary;
    text("experiment-binary", info?.configuration.sitl_binary);
    const pair = actions?.pair() || {};
    for (const role of ["baseline", "blackout"]) text("experiment-" + role + "-name", pair[role] || "No " + role + " selected");
  }
  function render() {
    const info = state.info, run = selected();
    text("experiment-connection", state.connected ? "Connected" : state.pollController ? "Refreshing" : "Unavailable");
    $("experiment-connection").parentElement.dataset.stale = String(!state.connected);
    text("experiment-root", info?.configuration.experiment_root);
    text("experiment-history-count", (info?.history.length || 0) + " / " + (info?.history_limit || 20));
    const history = $("experiment-history");
    const entries = info?.history || [];
    const signature = JSON.stringify(entries.map(run => [run.run_id, run.state]));
    if (history.dataset.signature !== signature) {
      history.replaceChildren();
      for (const run of entries) history.add(new Option(run.run_id + " / " + run.requested.scenario + " / " + run.state, run.run_id));
      if (!entries.length) history.add(new Option("No experiments", ""));
      history.dataset.signature = signature;
    }
    history.value = state.selected || "";
    text("experiment-run-id", run?.run_id || "No experiment selected");
    text("experiment-process-state", run?.state || "Idle");
    $("experiment-process-state").dataset.state = run?.state || "idle";
    text("experiment-outcome", run?.declared_outcome);
    text("experiment-elapsed", run?.elapsed_s);
    text("experiment-evidence-status", state.validated && run && state.validated.id === run.run_id
      ? state.validated.status + " (last read)" : "Not validated");
    text("experiment-output", run?.output);
    for (const [id, key] of [["source", "source"], ["scenario", "scenario"], ["duration", "duration_s"], ["blackout-at", "blackout_at_s"], ["blackout-duration", "blackout_duration_s"]]) {
      text("experiment-requested-" + id, run?.requested[key]);
    }
    text("experiment-clocks", run ? JSON.stringify({ created: run.created, stop_requested: run.stop_requested, finished: run.finished }, null, 2) : "No controller clocks");
    text("experiment-stop-reason", run?.stop_reason);
    text("experiment-returncode", run?.returncode);
    text("experiment-forced", run ? (run.forced_termination ? "Yes" : "No") : null);
    text("experiment-diagnostics", [run?.error, run?.diagnostics].filter(Boolean).join("\n\n") || "No diagnostics");
    const otherActive = info?.active_run_id && info.active_run_id !== run?.run_id;
    $("experiment-active-notice").hidden = !otherActive && state.connected;
    text("experiment-active-notice", !state.connected ? "Status unavailable / last received state shown" : "Active server run: " + info.active_run_id);
    controls();
  }
  function accept(data, target = null) {
    if (data.schema_version !== 6 || !Array.isArray(data.experiment?.history) || !data.experiment.action_token || !data.experiment.configuration) {
      throw new Error("The local service returned an unsupported experiment status.");
    }
    const info = data.experiment, head = info.history[0]?.run_id || null;
    const next = target || (state.followLatest || !info.history.some(run => run.run_id === state.selected) ? head : state.selected);
    if (next !== state.selected) { cancelHandoff(); state.validated = null; }
    state.selected = next;
    state.info = info; state.connected = true;
    render();
  }
  function cancelPoll() {
    clearTimeout(state.timer); state.poll++; state.pollController?.abort(); state.pollController = null;
  }
  function schedule() {
    clearTimeout(state.timer);
    if (state.visible && !state.busy) state.timer = setTimeout(refresh, 500);
  }
  async function refresh() {
    if (!state.visible || state.busy) return;
    cancelPoll();
    const request = state.poll, controller = new AbortController();
    state.pollController = controller; render();
    const timeout = setTimeout(() => controller.abort(), 5000);
    try {
      const response = await fetch("/api/experiment/status", { cache: "no-store", credentials: "same-origin", signal: controller.signal });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "Experiment status is unavailable.");
      if (request !== state.poll || !state.visible) return;
      accept(data);
    } catch {
      if (request !== state.poll || !state.visible) return;
      state.connected = false;
    } finally {
      clearTimeout(timeout);
      if (request === state.poll) { state.pollController = null; render(); schedule(); }
    }
  }
  function configuration() {
    const value = id => {
      const input = $(id), number = input.valueAsNumber;
      if (!Number.isFinite(number)) throw new Error("Requested durations must be finite numbers.");
      return number;
    };
    const config = { source: $("experiment-source").value, scenario: $("experiment-scenario").value, duration_s: value("experiment-duration") };
    if (config.scenario === "blackout") {
      config.blackout_at_s = value("experiment-blackout-at");
      config.blackout_duration_s = value("experiment-blackout-duration");
    }
    if (config.source === "arducopter-sitl") config.startup_timeout_s = value("experiment-startup-timeout");
    return config;
  }
  async function command(kind, body) {
    if (!state.connected || state.busy) return;
    cancelPoll(); cancelHandoff();
    const token = state.info.action_token;
    state.busy = true; error(null); render();
    const controller = new AbortController(), timeout = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch("/api/experiment/" + kind, { method: "POST", cache: "no-store", credentials: "same-origin", signal: controller.signal,
        headers: { "Content-Type": "application/json", "X-UAV-Debugger-Action": token }, body: JSON.stringify(body) });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "The experiment action was rejected.");
      if (kind === "start") state.followLatest = true;
      accept(data, data.run_id);
    } catch (failure) {
      state.connected = false;
      error(failure instanceof TypeError || failure.name === "AbortError" || failure instanceof SyntaxError
        ? "The action result is unknown. Refreshing server state; the action will not be retried."
        : failure.message);
    } finally {
      clearTimeout(timeout); state.busy = false; render();
      if (state.visible) refresh();
    }
  }
  function cancelHandoff() {
    state.handoff++; state.handoffController?.abort(); state.handoffController = null; state.handoffBusy = false;
    text("experiment-handoff-status", "");
  }
  async function handoff(destination) {
    const run = selected();
    if (!state.connected || state.busy || state.handoffBusy || !run?.can_open) return;
    cancelHandoff();
    const request = state.handoff, controller = new AbortController();
    state.handoffController = controller; state.handoffBusy = true; state.validated = null;
    error(null); text("experiment-handoff-status", "Validating saved evidence..."); controls();
    try {
      const response = await fetch("/api/experiment/open?" + new URLSearchParams({ run_id: run.run_id }), { cache: "no-store", credentials: "same-origin", signal: controller.signal });
      const data = await response.json();
      if (request !== state.handoff || !state.visible || run.run_id !== state.selected) return;
      if (!response.ok) throw new Error(data.error || "Saved evidence could not be opened.");
      if (data.schema_version !== 6 || !data.run || !data.catalog_key) throw new Error("The local service returned unsupported saved evidence.");
      state.validated = { id: run.run_id, status: data.run.evidence_status };
      text("experiment-handoff-status", destination === "analyze" ? "Saved evidence opened" : "Assigned as " + destination);
      if (destination === "analyze") actions.inspect(data.catalog_key);
      else actions.assign(destination, data.catalog_key, data.run);
    } catch (failure) {
      if (request !== state.handoff || failure.name === "AbortError") return;
      error(failure instanceof TypeError ? "The saved evidence could not be reached. Try opening it again." : failure.message);
      text("experiment-handoff-status", "Evidence unavailable / no handoff");
    } finally {
      if (request === state.handoff) { state.handoffBusy = false; state.handoffController = null; render(); }
    }
  }
  function init(callbacks) {
    actions = callbacks;
    $("experiment-refresh").addEventListener("click", refresh);
    for (const id of ["experiment-source", "experiment-scenario"]) $(id).addEventListener("change", controls);
    $("experiment-form").addEventListener("submit", event => {
      event.preventDefault();
      if ($("experiment-start").disabled) return;
      try { command("start", configuration()); } catch (failure) { error(failure.message); }
    });
    $("experiment-stop").addEventListener("click", () => {
      const run = selected();
      if (!$("experiment-stop").disabled && run) command("stop", { run_id: run.run_id });
    });
    $("experiment-history").addEventListener("change", () => {
      cancelHandoff(); state.selected = $("experiment-history").value; state.followLatest = false; state.validated = null; render();
    });
    $("experiment-open-analyze").addEventListener("click", () => handoff("analyze"));
    for (const role of ["baseline", "blackout"]) $("experiment-use-" + role).addEventListener("click", () => handoff(role));
    $("experiment-compare").addEventListener("click", () => { if (actions.hasPair()) actions.compare(); });
    $("experiment-catalog").addEventListener("click", actions.browse);
    render();
  }
  globalThis.InstrumentExperiment = { init, controls,
    enter: () => { state.visible = true; state.connected = false; render(); refresh(); },
    leave: () => { state.visible = false; state.connected = false; cancelPoll(); cancelHandoff(); },
  };
})();
