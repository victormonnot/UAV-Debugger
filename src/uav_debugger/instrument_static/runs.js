(() => {
  "use strict";
  const $ = id => document.getElementById(id);
  const text = (id, value) => { $(id).textContent = String(value ?? "Unavailable"); };
  const pointLabel = value => ({ "relay-input": "Relay input", receiver: "Receiver" })[value] || "Unavailable";
  const pageKeys = { gates: "gate_page", issues: "evidence_page", trace: "trace_page" };
  let actions, run = null, busy = false, plotRevision = 0, plotQueue = Promise.resolve();
  let catalog = null, catalogPage = 0, catalogRequest = 0, catalogController = null, catalogBusy = false;
  const CATALOG_PAGE_SIZE = 50;

  function rows(id, entries, values, decorate) {
    const fragment = document.createDocumentFragment();
    for (const item of entries) {
      const row = document.createElement("tr");
      for (const value of values(item)) {
        const cell = document.createElement("td");
        cell.textContent = String(value ?? "Unavailable"); row.append(cell);
      }
      decorate?.(row, item); fragment.append(row);
    }
    $(id).replaceChildren(fragment);
  }
  function renderPage(kind, page) {
    $(`run-${kind}-page`).value = String((page?.page || 0) + 1);
    $(`run-${kind}-page`).max = String(page?.page_count || 1);
    text(`run-${kind}-page-count`, page?.page_count || 1);
    $(`run-${kind}-pagination`).hidden = (page?.page_count || 1) <= 1;
  }
  function setBusy(value) {
    busy = value;
    for (const kind of Object.keys(pageKeys)) {
      const page = run?.[kind];
      $(`run-${kind}-previous`).disabled = busy || !page || page.page === 0;
      $(`run-${kind}-next`).disabled = busy || !page || page.page + 1 >= page.page_count;
      $(`run-${kind}-page`).disabled = busy || !page?.total_count;
    }
    $("run-trace-kind").disabled = busy || !run;
    $("catalog-refresh").disabled = busy || catalogBusy;
    document.querySelectorAll("#catalog-rows button").forEach(button => {
      button.disabled = busy || catalogBusy || button.dataset.openable !== "true";
    });
  }
  function render(value, reset = false) {
    run = value;
    $("run-tab").hidden = !run;
    text("run-outcome", run?.declared_outcome);
    text("run-evidence-status", run?.evidence_status);
    $("run-evidence-status").dataset.state = run?.evidence_status || "";
    text("run-schema", run?.schema);
    text("run-identity", run?.identity);
    text("run-requested", run?.requested_json || "");
    text("run-provenance", run?.provenance_json || "");
    text("run-measurement-time", run?.measurement_elapsed_s);
    $("run-warning").hidden = !run || run.evidence_status === "consistent";
    text("run-warning", run?.evidence_status === "invalid" ? "Invalid evidence / retained captures remain inspectable" : "Incomplete evidence / missing observations are not zero counts");
    rows("run-capture-rows", run?.captures || [], item => [pointLabel(item.point), item.record_count, item.observation_count, item.consistent_reference_count, item.traversal], (row, item) => { row.dataset.point = item.point; });
    rows("run-gate-rows", run?.gates.rows || [], item => [item.start_elapsed_s, item.end_elapsed_s, item.duration_s, item.start_reference, item.end_reference, item.end_reason]);
    $("run-gates-empty").hidden = Boolean(run?.gates.total_count);
    rows("run-issue-rows", run?.issues.rows || [], item => [item.line_number === null ? item.file_name : `${item.file_name}:${item.line_number}`, item.severity, item.code, item.message]);
    text("run-issue-count", run?.issues.total_count || 0);
    $("run-issues-empty").hidden = Boolean(run?.issues.total_count);
    $("run-issue-counts").replaceChildren();
    for (const [code, count] of Object.entries(run?.issues.counts || {})) {
      const term = document.createElement("dt"), value = document.createElement("dd");
      term.textContent = code; value.textContent = count; $("run-issue-counts").append(term, value);
    }
    rows("run-trace-rows", run?.trace.rows || [], item => [item.reference, item.action, pointLabel(item.point), item.record_index, item.offset, item.elapsed_s, item.unix_us, item.valid ? "Yes" : "No"]);
    text("run-trace-count", run?.trace.total_count || 0);
    $("run-trace-kind").value = run?.trace.kind || "actions";
    rows("run-files-rows", run?.files || [], item => [item.name, item.size_bytes, item.sha256]);
    $("run-ignored-files").replaceChildren(...(run?.ignored_files || []).map(name => {
      const item = document.createElement("li"); item.textContent = name; return item;
    }));
    $("run-ignored").hidden = !run?.ignored_files.length;
    for (const kind of Object.keys(pageKeys)) renderPage(kind, run?.[kind]);
    if (reset) for (const name of ["issues", "trace", "provenance"]) $(`run-${name}-details`).open = false;
    setBusy(busy);
    renderPlot();
  }
  function renderPlot() {
    const revision = ++plotRevision;
    plotQueue = plotQueue.catch(() => {}).then(async () => {
      if (revision !== plotRevision) return;
      const plot = $("run-timeline");
      if (!run) { globalThis.Plotly?.purge(plot); return; }
      if ($("run-view").hidden) return;
      const figure = run.timeline.figure;
      $("run-timeline").hidden = !figure;
      $("run-timeline-empty").hidden = Boolean(figure);
      text("run-timeline-empty", run.timeline.status === "too_many_intervals"
        ? `${run.timeline.interval_count} applied intervals / ${run.timeline.interval_limit}-interval display limit`
        : "No observations with consistent capture and clock references");
      if (!figure) { globalThis.Plotly?.purge(plot); return; }
      const styles = getComputedStyle(document.documentElement), color = name => styles.getPropertyValue(name).trim();
      const data = structuredClone(figure.data), layout = structuredClone(figure.layout);
      data.forEach((trace, index) => {
        trace.marker = { ...trace.marker, color: color(index === 0 ? "--activity" : "--roll"), opacity: 1 };
        trace.hoverlabel = { bgcolor: color("--field"), bordercolor: color("--control-line"), font: { color: color("--ink"), family: "IBM Plex Sans", size: 12 } };
      });
      layout.height = 310;
      layout.autosize = true; delete layout.width;
      layout.margin = { l: 50, r: 12, t: 36, b: 66 };
      layout.paper_bgcolor = layout.plot_bgcolor = color("--page");
      layout.font = { family: "IBM Plex Sans, sans-serif", size: 12, color: color("--ink") };
      layout.legend = { orientation: "h", x: 0, y: 1.15 };
      layout.uirevision = run.identity;
      for (const name of ["xaxis", "yaxis"]) {
        const axis = layout[name] || {};
        axis.gridcolor = color("--grid"); axis.zerolinecolor = color("--control-line");
        axis.tickfont = { color: color("--muted"), size: 11 };
        axis.title = { text: name === "xaxis" ? "Run elapsed (s)" : "Records / bin", font: { color: color("--ink"), size: 12 } };
        axis.fixedrange = matchMedia("(pointer: coarse)").matches;
        layout[name] = axis;
      }
      await Plotly.react(plot, data, layout, { displayModeBar: false, displaylogo: false, responsive: false, scrollZoom: false });
    }).catch(() => {
      if (revision !== plotRevision) return;
      $("run-timeline").hidden = true; $("run-timeline-empty").hidden = false;
      text("run-timeline-empty", "Run timeline could not be displayed");
    });
  }
  function cancelCatalog() {
    catalogRequest++; catalogController?.abort(); catalogController = null; catalogBusy = false;
    $("catalog-view").setAttribute("aria-busy", "false");
    setBusy(busy);
  }
  async function loadCatalog() {
    cancelCatalog();
    const request = catalogRequest, controller = new AbortController();
    catalogController = controller; catalogBusy = true;
    $("catalog-error").hidden = true;
    $("catalog-view").setAttribute("aria-busy", "true"); setBusy(busy);
    try {
      const response = await fetch("/api/catalog", { cache: "no-store", credentials: "same-origin", signal: controller.signal });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "The catalog could not be read.");
      if (data.schema_version !== 5 || !Array.isArray(data.entries)) throw new Error("The local service returned an unsupported catalog.");
      if (request !== catalogRequest) return;
      catalog = data; catalogPage = 0; renderCatalog();
    } catch (error) {
      if (request !== catalogRequest || error.name === "AbortError") return;
      catalog = null; renderCatalog();
      text("catalog-error", error instanceof TypeError ? "The local catalog service could not be reached." : error.message);
      $("catalog-error").hidden = false;
    } finally {
      if (request === catalogRequest) {
        catalogBusy = false; catalogController = null;
        $("catalog-view").setAttribute("aria-busy", "false"); setBusy(busy);
      }
    }
  }
  function renderCatalog() {
    if (catalog) text("catalog-root", catalog.root);
    $("catalog-partial").hidden = !catalog?.truncated;
    $("catalog-issues").replaceChildren(...(catalog?.issues || []).map(message => {
      const item = document.createElement("li"); item.textContent = message; return item;
    }));
    const entries = catalog?.entries || [];
    rows("catalog-rows", entries.slice(catalogPage * CATALOG_PAGE_SIZE, (catalogPage + 1) * CATALOG_PAGE_SIZE), item => [
      item.key, item.source, item.scenario, item.outcome, item.duration_s, item.start_unix_us, item.issue || "Not yet validated",
    ], (row, item) => {
      row.dataset.key = item.key;
      const cell = document.createElement("td"), button = document.createElement("button");
      button.className = "icon-button bordered"; button.dataset.key = item.key; button.dataset.openable = String(item.openable);
      button.setAttribute("aria-label", `Open ${item.key} in Analyze`); button.title = `Open ${item.key} in Analyze`;
      const icon = document.createElement("i"); icon.dataset.lucide = "folder-open"; icon.setAttribute("aria-hidden", "true"); button.append(icon);
      button.addEventListener("click", () => actions.openCatalog(item.key)); cell.append(button);
      for (const role of ["baseline", "blackout"]) {
        const assign = document.createElement("button"), icon = document.createElement("i");
        assign.className = "icon-button bordered"; assign.dataset.role = role; assign.dataset.openable = String(item.openable);
        assign.setAttribute("aria-label", "Use " + item.key + " as " + role); assign.title = "Use as " + role;
        icon.dataset.lucide = role === "baseline" ? "bookmark-plus" : "zap"; icon.setAttribute("aria-hidden", "true"); assign.append(icon);
        assign.addEventListener("click", () => actions.assign(role, item.key)); cell.append(assign);
      }
      row.append(cell);
    });
    if (globalThis.lucide) lucide.createIcons();
    const pages = Math.max(1, Math.ceil(entries.length / CATALOG_PAGE_SIZE));
    text("catalog-page", `${catalogPage + 1} / ${pages} / ${entries.length} entries`);
    $("catalog-empty").hidden = entries.length > 0;
    $("catalog-previous").disabled = catalogPage === 0;
    $("catalog-next").disabled = catalogPage + 1 >= pages;
    setBusy(busy);
  }
  function init(callbacks) {
    actions = callbacks;
    const tab = document.createElement("button"); tab.id = "run-tab"; tab.type = "button"; tab.textContent = "Run"; tab.hidden = true; tab.tabIndex = -1;
    tab.setAttribute("role", "tab"); tab.setAttribute("aria-selected", "false"); tab.setAttribute("aria-controls", "run-view");
    $("activity-tab").before(tab);
    for (const [kind, parameter] of Object.entries(pageKeys)) {
      const parent = $(`run-${kind}-pagination`);
      const change = page => {
        if (!run || busy || !Number.isSafeInteger(page) || page < 0 || page >= run[kind].page_count || page === run[kind].page) {
          renderPage(kind, run?.[kind]); return;
        }
        actions.page({ [parameter]: page });
      };
      for (const [direction, step] of [["previous", -1], ["next", 1]]) {
        const button = document.createElement("button"), icon = document.createElement("i");
        button.id = `run-${kind}-${direction}`; button.className = "icon-button bordered"; button.type = "button";
        button.setAttribute("aria-label", `${direction === "previous" ? "Previous" : "Next"} ${kind}`); button.dataset.tip = button.getAttribute("aria-label");
        icon.dataset.lucide = step < 0 ? "chevron-left" : "chevron-right"; icon.setAttribute("aria-hidden", "true"); button.append(icon);
        button.addEventListener("click", () => change(run[kind].page + step)); parent.append(button);
      }
      const controls = document.createElement("div"), label = document.createElement("label"), input = document.createElement("input"), count = document.createElement("span");
      controls.className = "page-control"; label.textContent = "Page"; label.htmlFor = `run-${kind}-page`;
      input.id = label.htmlFor; input.type = "number"; input.min = "1"; input.max = "1"; input.value = "1";
      input.setAttribute("aria-label", `${kind} page`); count.id = `run-${kind}-page-count`;
      controls.append(label, input, document.createTextNode(" / "), count); parent.lastChild.before(controls);
      input.addEventListener("change", () => change(Number(input.value) - 1));
      input.addEventListener("keydown", event => { if (event.key === "Enter") { event.preventDefault(); change(Number(input.value) - 1); } });
    }
    $("run-trace-kind").addEventListener("change", () => actions.page({ trace: $("run-trace-kind").value, trace_page: 0 }));
    $("catalog-refresh").addEventListener("click", loadCatalog);
    $("catalog-previous").addEventListener("click", () => { catalogPage--; renderCatalog(); });
    $("catalog-next").addEventListener("click", () => { catalogPage++; renderCatalog(); });
    let timer;
    new ResizeObserver(() => { clearTimeout(timer); timer = setTimeout(renderPlot, 80); }).observe($("run-timeline"));
    document.fonts.ready.then(renderPlot);
  }
  globalThis.InstrumentRuns = { init, render, renderPlot, setBusy, loadCatalog, cancelCatalog,
    configure: config => text("catalog-root", config.experiment_root),
    resetChart: () => { if (run?.timeline.figure) return Plotly.relayout($("run-timeline"), { "xaxis.autorange": true, "yaxis.autorange": true }); },
  };
})();
