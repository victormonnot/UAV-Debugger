# Inspect a saved Experiment

Development version **0.2.0rc2.dev0** includes offline inspection of one saved
Experiment directory in Analyze. It brings requested settings, applied actions
and observed captures into one view and report. The published v0.1.0 remains
single-recording Analyze; the development version is unpublished. **Local
experiments** additionally browses saved directories on the server, including
after restart, without an upload or an active runner.

The reader supports the actual `uav-debugger-experiment-v1` synthetic format and
`uav-debugger-experiment-v2` pinned SITL format. Neither a simulator installation
nor an active runner is required. Opening saved evidence never executes paths or
commands from its manifest, opens a telemetry transport or resumes an experiment.

## Browse local experiments

1. Start the local application with the output root to inspect, for example:

   ```sh
   uv run --locked uav-debugger-analyze --experiment-root local/experiments
   ```

2. In Analyze, choose **Local experiments** under **Analyze input**. The
   **Browse saved experiments** button in Experiment opens the same view.
3. Review the listed directories and their **declared** manifest metadata.
   Listing a run does not validate its captures, counters, hashes or references.
4. Select an entry under **Saved run** and choose **Open in Analyze** to read the complete evidence
   through the existing saved-run validator. Inspect **Declared outcome** and
   **Evidence status** independently, then use the normal capture views/report.
5. To compare two entries, use **Use as baseline** and **Use as blackout**, then
   **Compare selected runs**. Both runs are reread and validated; compatibility
   and observation metrics follow the existing [comparison contract](comparison.md).
6. Use **Refresh catalog** when the directories or saved manifests change.
   The view is a snapshot of declared metadata, not a live telemetry monitor.

The root is a server-side path supplied by `--experiment-root`; its default is
`local/experiments` relative to the launch directory. With SSH access, browsing
reads files on the server, while directory uploads select files on the browser
computer. An absent root gives an empty catalog with an explicit issue and does
not create a directory.

The catalog recognizes only the existing layouts directly below that root:

```text
<root>/<name>/run.json            # CLI evidence directory
<root>/<name>/evidence/run.json   # browser evidence directory
```

The root-relative evidence directory identifies each entry, so a CLI run named
`baseline` and a browser run named `run-...` remain distinguishable. There is no
database, persistent index, automatic deletion, arbitrary recursive search or
execution recovery. The Experiment controller's latest-20-run memory history
remains a separate view of launches owned by the current server.
If both supported layouts exist below the same direct child, they appear as
separate entries, such as `baseline` and `baseline/evidence`.

**Recorded start** is the manifest's declared host Unix timestamp in
microseconds. It is not file modification time and does not establish an ordering
of actions across runs. Requested duration is configuration, not an observed
elapsed duration or a capture count.

A manifest declaring `running` is shown as unfinalized and cannot be opened
through the catalog. This does not establish whether any process is alive.
Unreadable manifests or unsupported schemas/outcomes retain an explicit problem
and cannot be opened. Terminal runs can still contain missing or inconsistent artifacts;
opening them preserves readable evidence with the reader's issues where
possible. An unreadable evidence set is rejected. Outer `control.json` values
are ignored: stale controller state does not become a reconstructed live status.
Directories with absent manifests, symbolic links, unreadable paths or special
files produce explicit issues; browsing does not follow links or read FIFOs.

The scan examines at most **200 direct children**, including non-directory
entries, and reads at most **1 MiB per manifest** and **8 MiB of manifest bytes
in total**. An oversized manifest produces an entry issue. If the child limit or
total read budget prevents further scanning, the view identifies the catalog as
partial. Filesystem enumeration determines the scanned prefix; retained entries
are sorted by their relative keys. Browsing does not import captures or read the
action/observation traces. Full opening uses the independent saved-run limits
below and may reject evidence that had readable listing metadata.

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

Runs launched in the [Experiment interface](experiment-ui.md) offer **Open in
Analyze** after worker termination. This reads the server's retained evidence
through the same validator without uploading it. For later manual opening,
select `run-<identifier>/evidence`, which contains `run.json`. The outer
`control.json` stores controller requests and process state separately; it is not
part of the saved-run fingerprint or report. **Clear experiment** releases a
handoff selection as well as an uploaded selection; it never deletes files.

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
This single-run view does not align runs. Use [Compare experiments](comparison.md)
for an explicit baseline/blackout window. There are no active
Experiment controls in this view.

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

## Compare a baseline and blackout

Choose **Compare experiments** for a [bounded comparison](comparison.md) of two
saved directories. It checks evidence and profile compatibility before calculating
observed differences over an explicit common measurement-relative window.
