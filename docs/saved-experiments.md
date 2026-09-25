# Inspect a saved Experiment

Development version **0.2.0.dev2** adds offline inspection of one saved
Experiment directory in Analyze. It brings requested settings, applied actions
and observed captures into one view and report. The published v0.1.0 remains
single-recording Analyze; this capability is unreleased.

The reader supports the actual `uav-debugger-experiment-v1` synthetic format and
`uav-debugger-experiment-v2` pinned SITL format. Neither a simulator installation
nor an active runner is required. Opening saved evidence never executes paths or
commands from its manifest, opens a telemetry transport or resumes an experiment.

## Open and inspect

1. Start [Analyze](analyze.md#install-and-launch) and choose **Saved experiment**
   under **Analyze input**.
2. Under **Open saved experiment**, select the run directory containing
   `run.json`, the JSONL traces and the captures. Select the directory on the
   computer running the browser; transfer the saved directory there first when
   accessing Analyze through SSH. The directory uploader includes subdirectories.
3. Review **Declared outcome** and **Evidence status** independently. A runner
   reporting `completed` does not override a missing file or inconsistent hash.
4. Read **Requested settings**, **Applied forwarding interruption** and
   **Observed at the two capture points**. Applied intervals cite the actual
   action lines; observation counts come from the supplied captures and traces.
5. Inspect **Run timeline**. Startup, measurement origin, actual gate intervals
   and counts at both capture points use the run's host monotonic clock.
6. Choose **Relay input** or **Receiver** under **Observation point**. Use the
   existing source/type/time filters, attitude plot and record inspector for that
   capture. Changing point resets its filters and inspected record.
7. **Download report** exports the run evidence summary and the selected
   capture's applied filters and inspected record. **Clear experiment** removes
   the uploaded run; choose **Recording** for an ordinary `.tlog` or the example.

Select one run at a time. The directory uploader can add files to an existing
selection, so clear the current experiment before selecting another directory.
Combining directories or duplicate filenames is rejected. An invalid replacement
removes the previous report rather than continuing to export stale evidence.
Browser directory selection follows the
[Streamlit directory-upload contract](https://docs.streamlit.io/1.63.0/develop/api-reference/widgets/st.file_uploader).

## What is checked

The fixed evidence names are `run.json`, `actions.jsonl`, `observations.jsonl`,
`relay-input.tlog`, `receiver.tlog`, and, for SITL, `datagrams.jsonl`,
`simulator.log` and `simulator/profile.parm`. Other simulator working files are
not needed for this analysis. Other uploaded files, such as simulator terrain and persistent state, are
listed as excluded from analysis. Only the fixed evidence names contribute to
the run fingerprint and report; the reader never follows paths supplied by
the manifest. Upload count and total-byte limits include all selected files.

The reader checks supplied artifacts against declared byte sizes and SHA-256
fingerprints, then imports captures through the existing MAVLink importer.
Observation references must agree with capture point, record index, byte offset,
frame size, frame hash and capture timestamp. SITL references additionally link
the original frame bytes to the observed datagram and frame offset. Trace clock
arithmetic, action references and available final counters are checked as well.

A mismatched trace fingerprint excludes its events from the consistent timeline.
A mismatched capture fingerprint excludes observations referring to that capture;
the original capture remains independently inspectable. Missing final hashes
in an unfinalized run are reported, with internally consistent references retained.
These are consistency checks against supplied evidence, not sender authentication
or proof that a manifest is truthful. Opaque MAVLink records retain the importer's
unverified definition/checksum status even when their stored byte references agree.

**Evidence status** is `consistent`, `incomplete` or `invalid`. It describes the
reader's checks, independently of the runner's `completed`, `interrupted`,
`failed` or `running` outcome. For example, a correctly saved failed run may have
consistent evidence. A manifest still marked `running` is not proof that any
process is currently active. Missing evidence and a malformed trailing JSONL
record retain the readable prefix with explicit issues. An unsupported schema,
unreadable manifest or exceeded input bound rejects the run; individual `.tlog`
files can still be opened through the ordinary recording workflow.

## Clocks and interpretation

The run timeline counts all observation references that pass the available
consistency checks in at most 200 bins. It is independent of the capture filters
below. Gray shading marks recorded startup, a dotted line marks measurement
start, and red shading marks a gate interval bounded by actual disable/enable
actions. An unclosed interval has a start marker and no established duration.
No requested duration is substituted for a missing enable action.

Synthetic v1 uses the monotonic run origin as its measurement origin, including
older manifests without a `measurement_start` field. SITL v2 uses its recorded
measurement start after readiness; absent readiness leaves it unavailable.
Original event nanoseconds remain in the evidence and report; plotting seconds
does not change the stored clocks.

The capture inspector still filters against Unix capture timestamps relative
to that file's first record. Its activity and attitude plots are not silently
aligned to the run timeline. Host wall-clock regressions and device
`time_boot_ms` remain separate. Counts at two points and adjacent events do not
establish physical packet loss, exact transport latency or autopilot behavior.
There is no cross-run alignment, automatic baseline comparison or active
Experiment control in this view.

## Bounds and retained references

- At most **64 MiB** of supplied bytes per run and **64 uploaded files**.
- Each capture retains the existing **10 MiB** importer limit.
- `run.json` and the parameter profile: **1 MiB** each; actions and observations:
  **16 MiB** each; datagrams: **33 MiB**; simulator log: **8 MiB**.
- Each JSONL trace: at most **100,000 events**, with **1 MiB** per JSON line.
  Exceeded trace bounds retain the accepted prefix and report the stopping issue.
- Evidence issues and trace tables are paged in groups of **100**. The report
  explicitly identifies omitted detail when its bounded lists are exceeded.

Byte limits do not guarantee total memory consumption or processing time.
JSONL line numbers are one-based; capture indices and byte offsets are zero-based.
The run fingerprint identifies the accepted fixed-name evidence set. Original bytes are
never rewritten, and the Markdown report is a derived summary, not a run archive.
Retain the original directory alongside an exported report.

## Python API

```python
from pathlib import Path

from uav_debugger.run_report import build_run_markdown_report
from uav_debugger.saved_run import load_run_directory

run = load_run_directory(Path("local/experiments/blackout"))
print(run.declared_outcome, run.evidence_status)
report = build_run_markdown_report(run, point="receiver")
Path("local/blackout-report.md").write_text(report, encoding="utf-8")
```

`load_run_files(mapping, source_name=...)` accepts the same fixed names mapped to
bytes for uploaded evidence. `load_run_directory` reads only fixed artifact paths
and rejects symlinks and non-regular files. It does not import the execution
modules. The analysis and report APIs remain independent of Streamlit.
See [verification](verification.md) for actual checks and reproduction.
