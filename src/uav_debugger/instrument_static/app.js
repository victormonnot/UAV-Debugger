(() => {
  "use strict";
  const $ = id => document.getElementById(id);
  const state = { recording:null, source:0, visible:[true,true,true], request:0, controller:null, render:0, focused:false };
  let plotQueue = Promise.resolve();
  let classicURL = null;
  const media = matchMedia("(prefers-color-scheme: dark)");
  const themeSelect = $("theme-select");
  themeSelect.value = document.documentElement.dataset.appearance || "system";
  const text = (id,value) => { $(id).textContent = String(value ?? "N/A"); };

  function applyTheme() {
    const preference=themeSelect.value;
    document.documentElement.dataset.appearance=preference;
    document.documentElement.dataset.theme=preference === "system" ? (media.matches?"dark":"light") : preference;
    try { localStorage.setItem("uav-debugger.appearance",preference); } catch { /* Storage is optional. */ }
    renderPlot();
  }
  themeSelect.addEventListener("change",applyTheme);
  media.addEventListener("change",()=>{ if(themeSelect.value==="system") applyTheme(); });
  addEventListener("storage",event=>{
    if(event.key!=="uav-debugger.appearance") return;
    themeSelect.value=["light","dark","system"].includes(event.newValue)?event.newValue:"system";
    document.documentElement.dataset.appearance=themeSelect.value;
    document.documentElement.dataset.theme=themeSelect.value==="system"?(media.matches?"dark":"light"):themeSelect.value;
    renderPlot();
  });

  function status(message,kind="ready") {
    text("status-message",message);
    $("status").dataset.state=kind;
  }
  function sourceView() { return state.recording?.sources[state.source]; }
  function resetView() {
    state.visible=[true,true,true];
    document.querySelectorAll("[data-series]").forEach(button=>button.setAttribute("aria-pressed","true"));
    $("observations-details").open=false;
  }
  function exitFocus() {
    state.focused=false;
    document.body.classList.remove("is-focused");
    $("focus-chart").setAttribute("aria-pressed","false");
  }
  function clearRecording() {
    state.request++;
    state.controller?.abort();
    state.controller=null;
    state.recording=null;
    state.source=0;
    exitFocus();
    resetView();
    $("error").hidden=true;
    $("load-example").disabled=false;
    $("load-example").querySelector("span").textContent="Load example";
    renderRecording();
    status("Ready");
  }
  async function loadExample() {
    const request=++state.request;
    state.controller?.abort();
    const controller=new AbortController();
    state.controller=controller;
    state.recording=null;
    exitFocus();
    resetView();
    renderRecording();
    $("error").hidden=true;
    $("load-example").disabled=true;
    $("clear-recording").disabled=false;
    $("load-example").querySelector("span").textContent="Loading example";
    status("Reading telemetry-gap.tlog...","loading");
    try {
      const response=await fetch("/api/example",{signal:controller.signal,cache:"no-store",credentials:"same-origin"});
      if(!response.ok) throw new Error("Recording could not be loaded.");
      const data=await response.json();
      if(data.schema_version!==1 || !data.recording || !Array.isArray(data.sources) || !Array.isArray(data.issues)) throw new Error("The server returned an unsupported recording response.");
      if(request!==state.request) return;
      state.recording=data;
      state.source=0;
      renderRecording();
      const r=data.recording;
      status(`${r.record_count} records imported / ${data.issues.length} import issues`);
    } catch(error) {
      if(request!==state.request || error.name==="AbortError") return;
      state.recording=null;
      renderRecording();
      text("error-message",error.message==="The server returned an unsupported recording response."?error.message:"The example could not be loaded. Check the local service and retry.");
      $("error").hidden=false;
      status("Import failed","error");
    } finally {
      if(request===state.request) {
        state.controller=null;
        $("load-example").disabled=false;
        $("load-example").querySelector("span").textContent=state.recording?"Reload example":"Load example";
        $("clear-recording").disabled=!state.recording;
      }
    }
  }
  function renderRecording() {
    const data=state.recording,r=data?.recording;
    $("source-list").replaceChildren();
    $("source-empty").hidden=Boolean(data);
    $("clear-recording").disabled=!data;
    text("recording-kind",r?.synthetic?"Synthetic example":"No input");
    text("recording-name",r?r.source_name:"No recording selected");
    text("recording-meta",r?`${r.capture_span_s} s capture span / ${r.size_bytes} bytes / MAVLink 1 + 2`:"QGC timestamped MAVLink");
    text("record-count",r?.record_count??0);text("source-count",r?.source_count??0);
    text("decoded-count",r?.decoded_count??0);text("opaque-count",r?.opaque_count??0);
    text("import-status",r?({complete:"Complete import",stopped:"Partial import",empty:"Empty recording"}[r.traversal]||r.traversal):"No import");
    text("import-description",r?.synthetic?"Bundled synthetic recording":"Original recording");
    text("input-size",r?`${r.size_bytes} B`:null);text("consumed-size",r?`${r.consumed_bytes} B`:null);text("remaining-size",r?`${r.remaining_bytes} B`:null);
    text("capture-span",r?`${r.capture_span_s} s`:null);text("capture-origin",r?.capture_origin_us);
    text("profile",r?.profile);text("dialect",r?.dialect);text("decoder",r?.decoder_version);text("sha256",r?.sha256);
    text("footer-state",r?.synthetic?"SYNTHETIC EXAMPLE / FILE-ONLY":"FILE-ONLY ANALYSIS");
    if(data) data.sources.forEach((source,index)=>{
      const button=document.createElement("button");button.className="source-button";button.dataset.source=`${source.system_id}:${source.component_id}`;button.setAttribute("aria-pressed",String(index===state.source));
      const title=document.createElement("strong");title.textContent=`${source.system_id} / ${source.component_id}`;
      const count=document.createElement("small");count.textContent=`${source.record_count} records / ${source.attitude.summary.record_count} attitude`;
      button.append(title,count);button.addEventListener("click",()=>{state.source=index;resetView();document.querySelectorAll(".source-button").forEach(b=>b.setAttribute("aria-pressed",String(b===button)));renderSelection();});$("source-list").append(button);
    });
    const issues=data?.issues??[];text("issue-count",issues.length);$("issue-list").replaceChildren();$("issues-empty").hidden=Boolean(issues.length);text("issues-empty",r?"No issues reported":"No import");
    issues.forEach(issue=>{
      const item=document.createElement("li"),code=document.createElement("strong"),message=document.createElement("p"),reference=document.createElement("small");
      code.textContent=issue.code;message.textContent=issue.message;reference.textContent=`Record #${issue.record_index} / byte ${issue.offset}`;item.append(code,message,reference);$("issue-list").append(item);
    });
    if(!data) {$("issues-details").open=false;$("provenance-details").open=false;}
    renderSelection();
  }
  function renderSelection() {
    const view=sourceView(),summary=view?.attitude.summary,ready=summary?.status==="ready"&&Boolean(view?.attitude.figure);
    text("sample-count",summary?.record_count??0);
    text("plot-context",view?`Source ${view.system_id} / ${view.component_id}`:"No source selected");
    $("empty-plot").hidden=Boolean(state.recording);$("attitude-plot").hidden=!ready;$("plot-unavailable").hidden=!state.recording||ready;
    text("plot-unavailable-text",summary?.status==="too_many"?"Display limit reached":"No attitude observations");
    document.querySelectorAll("[data-series]").forEach(b=>b.disabled=!ready);
    $("reset-chart").disabled=!ready;$("focus-chart").disabled=!ready;
    $("observations-details").hidden=!ready;
    renderObservationTable(view);
    const references=view?.attitude.figure?.data?.[0]?.customdata?.filter(Boolean)??[];
    let largestGap=null;
    for(let i=1;i<references.length;i++) {
      const interval=BigInt(references[i][1])-BigInt(references[i-1][1]);
      if(interval>BigInt(summary.max_gap_us)&&(!largestGap||interval>largestGap.interval)) largestGap={interval,before:references[i-1][0],after:references[i][0]};
    }
    $("gap-caption").hidden=!largestGap;
    text("gap-caption",largestGap?`${(Number(largestGap.interval)/1e6).toFixed(3)} s between records #${largestGap.before} and #${largestGap.after}. No line across the gap.`:"");
    text("plot-summary",ready?`${summary.plotted_record_count} ATTITUDE observations / line gap limit ${(summary.max_gap_us/1e6).toFixed(3)} s`:"Capture time relative to the first recorded message.");
    renderPlot();
  }
  function renderObservationTable(view) {
    $("observation-rows").replaceChildren();
    const traces=view?.attitude.figure?.data??[],records=new Map();
    traces.forEach((trace,field)=>trace.customdata?.forEach((ref,i)=>{
      if(!ref)return;
      const row=records.get(ref[0])||{index:ref[0],offset:ref[2],timestamp:ref[1],values:[null,null,null]};
      row.values[field]=trace.y[i];records.set(ref[0],row);
    }));
    text("observations-count",records.size);
    records.forEach(record=>{
      const row=document.createElement("tr");
      [`#${record.index}`,record.offset,...record.values.map(v=>v===null?"N/A":Number(v).toFixed(3)),record.timestamp].forEach(value=>{const cell=document.createElement("td");cell.textContent=String(value);row.append(cell);});
      $("observation-rows").append(row);
    });
  }
  function renderPlot() {
    const revision=++state.render;
    plotQueue=plotQueue.catch(()=>{}).then(async()=>{
      if(revision!==state.render)return;
      const source=sourceView(),figure=source?.attitude.figure,plot=$("attitude-plot");
      if(!figure||source.attitude.summary.status!=="ready") {if(globalThis.Plotly)Plotly.purge(plot);return;}
      if(!globalThis.Plotly)throw new Error("The local chart library is unavailable.");
      const styles=getComputedStyle(document.documentElement),color=name=>styles.getPropertyValue(name).trim();
      const data=structuredClone(figure.data),layout=structuredClone(figure.layout),colors=[color("--roll"),color("--pitch"),color("--yaw")];
      data.forEach((trace,i)=>{trace.line={...trace.line,color:colors[i],width:2};trace.marker={...trace.marker,color:colors[i],size:7};trace.visible=state.visible[i];trace.hoverlabel={bgcolor:color("--field"),bordercolor:color("--control-line"),font:{color:color("--ink"),family:"IBM Plex Sans",size:12}};});
      layout.height=plot.clientHeight||465;layout.autosize=true;delete layout.width;
      layout.margin={l:66,r:24,t:22,b:64};layout.font={family:"IBM Plex Sans, sans-serif",size:12,color:color("--ink")};
      layout.paper_bgcolor=color("--page");layout.plot_bgcolor=color("--page");layout.showlegend=false;layout.uirevision=`${source.system_id}:${source.component_id}`;
      layout.clickmode="event";
      Object.entries(layout).forEach(([name,axis])=>{if(!/^[xy]axis\d*$/.test(name))return;axis.gridcolor=color("--grid");axis.zerolinecolor=color("--control-line");axis.tickfont={color:color("--muted"),size:12};axis.title={...axis.title,font:{color:color("--ink"),size:12}};axis.fixedrange=matchMedia("(pointer: coarse)").matches;});
      await Plotly.react(plot,data,layout,{displayModeBar:false,displaylogo:false,responsive:false,scrollZoom:false,showLink:false,locale:"en"});
    }).catch(()=>{
      if(revision!==state.render)return;
      $("attitude-plot").hidden=true;$("plot-unavailable").hidden=false;text("plot-unavailable-text","Chart unavailable");text("error-message","The chart could not be displayed. Retry the example.");$("error").hidden=false;
    });
  }
  let resizeTimer;
  new ResizeObserver(()=>{clearTimeout(resizeTimer);resizeTimer=setTimeout(renderPlot,80);}).observe($("attitude-plot"));
  document.fonts.ready.then(renderPlot);
  document.querySelectorAll("[data-series]").forEach(button=>button.addEventListener("click",()=>{const i=Number(button.dataset.series);state.visible[i]=!state.visible[i];button.setAttribute("aria-pressed",String(state.visible[i]));renderPlot();}));
  $("reset-chart").addEventListener("click",()=>{
    if(!sourceView()?.attitude.figure||!globalThis.Plotly)return;
    plotQueue=plotQueue.catch(()=>{}).then(()=>{if(!state.recording)return;return Plotly.relayout($("attitude-plot"),{"xaxis.autorange":true,"xaxis2.autorange":true,"xaxis3.autorange":true,"yaxis.autorange":true,"yaxis2.autorange":true,"yaxis3.autorange":true});});
  });
  $("focus-chart").addEventListener("click",()=>{state.focused=!state.focused;document.body.classList.toggle("is-focused",state.focused);$("focus-chart").setAttribute("aria-pressed",String(state.focused));renderPlot();});
  $("load-example").addEventListener("click",loadExample);$("retry-example").addEventListener("click",loadExample);$("clear-recording").addEventListener("click",clearRecording);

  function openWorkspace(experiment) {
    text("workspace-title",experiment?"Experiment":"Existing workspace");
    text("workspace-context",experiment?"Execution is available in the existing workspace.":"Personal recordings, reports and saved experiments");
    text("workspace-availability",classicURL?"Separate local service / no recording is transferred.":"No existing workspace linked.");
    $("classic-link").hidden=!classicURL;
    if(classicURL)$("classic-link").href=classicURL;
    $("workspace-dialog").showModal();
  }
  $("experiment-button").addEventListener("click",()=>openWorkspace(true));$("full-workspace").addEventListener("click",()=>openWorkspace(false));$("close-workspace").addEventListener("click",()=>$("workspace-dialog").close());
  $("workspace-dialog").addEventListener("click",event=>{if(event.target!==$("workspace-dialog"))return;const r=event.target.getBoundingClientRect();if(event.clientX<r.left||event.clientX>r.right||event.clientY<r.top||event.clientY>r.bottom)event.target.close();});
  fetch("/api/config",{cache:"no-store",credentials:"same-origin"}).then(r=>{if(!r.ok)throw new Error();return r.json();}).then(config=>{
    text("version",`v${config.version}`);
    if(config.classic_url) {const url=new URL(config.classic_url);if(url.protocol==="http:"&&["127.0.0.1","localhost","[::1]"].includes(url.hostname)){url.hostname=location.hostname;classicURL=url.href;}}
  }).catch(()=>{text("version","Service unavailable");});

  const tooltip=$("tooltip");
  function hideTooltip(){tooltip.hidden=true;}
  function showTooltip(button){if(button.disabled)return;tooltip.textContent=button.dataset.tip;tooltip.hidden=false;const r=button.getBoundingClientRect(),t=tooltip.getBoundingClientRect();tooltip.style.left=Math.max(8,Math.min(innerWidth-t.width-8,r.left+r.width/2-t.width/2))+"px";tooltip.style.top=(r.bottom+8+t.height<innerHeight?r.bottom+8:r.top-t.height-8)+"px";}
  document.querySelectorAll("[data-tip]").forEach(button=>{button.addEventListener("mouseenter",()=>showTooltip(button));button.addEventListener("focus",()=>showTooltip(button));button.addEventListener("mouseleave",hideTooltip);button.addEventListener("blur",hideTooltip);button.addEventListener("click",hideTooltip);});
  addEventListener("scroll",hideTooltip,true);addEventListener("keydown",e=>{if(e.key==="Escape")hideTooltip();});
  if(globalThis.lucide)lucide.createIcons();
  renderRecording();
  if(new URLSearchParams(location.search).get("example")==="1")loadExample();
})();
