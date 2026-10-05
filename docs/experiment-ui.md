# Run an Experiment from the local interface

Version **0.2.0** provides explicit **Experiment** controls beside
the independent **Analyze** mode. Start a bounded synthetic or pinned ArduCopter
SITL run, stop it if needed, then open its saved observations in Analyze or
compare a baseline and blackout. The commands below use the 0.2.0 source checkout.

The source checkout also provides these controls in [Instrument](instrument.md).
Its custom interface is not included in the published v0.2.0 artifacts. Both
interfaces use the same controller, worker and runner contracts; an individual
server owns its own controller and history.

## Use Instrument

From the repository root:

```sh
uv sync --locked
uv run --locked uav-debugger-instrument --experiment-root local/experiments
```

Open [Instrument](http://127.0.0.1:8765) and choose **Experiment**. Analyze remains
independently usable without starting a controller or a worker. Merely opening
Experiment or refreshing its status creates no output directory.

1. Choose **Synthetic**, **Baseline** and a six-second duration, then explicitly
   choose **Start experiment**. A valid Start creates a new owned run under the
   configured output root. Invalid settings are rejected before creating output.
2. Review **Controller state**, **Declared outcome** and **Controller elapsed**.
   Status is refreshed while the Experiment view is visible; it is not a live
   telemetry plot or evidence-validation result. Another tab's active run uses
   the same server controller and prevents a second simultaneous launch.
3. Choose **Stop experiment** when orderly interruption is needed. The request
   names the displayed run. A delayed request for an older terminal run cannot
   stop a newly launched worker, and repeating Stop preserves its original
   request time. Stop requested does not mean the worker has exited yet.
4. After worker termination, choose a run under **Selected experiment**, inspect
   its settings, controller clocks and diagnostics, then **Open in Analyze**. The service
   validates saved files before handing them to the saved-run view. A controller
   outcome alone does not establish consistent evidence.
5. Use **Use as baseline** or **Use as blackout** for a terminal run, then
   **Compare selected runs** to open the existing file-only comparison. Role
   assignment validates evidence without asserting pair eligibility.

Returning to Analyze preserves the tab's previous recording unless an explicit
saved-run handoff replaces it. Mode changes, page reload and browser closure
do not stop a worker. **Browse saved experiments** opens the separate read-only
catalog; after server restart, it can discover files without restoring old
controller history or claiming a process is still alive.

Choosing an older history entry preserves that selection when another tab
starts a run; the active run remains explicitly identified. A failed status
refresh marks the displayed snapshot as stale and disables Start/Stop until
fresh state arrives. Evidence starts as **Not validated**; a successful handoff
shows the reader's result marked **(last read)**, not continuous file validation.

If a Start or Stop response is lost, the command may already have reached the
server. Instrument refreshes status and never automatically repeats Start.
Check the resulting process state before issuing another command. The local
action token and same-origin checks protect command requests, but they are not
user authentication or a remote multi-user execution service.

The source/scenario settings, output layout, lifecycle limits and simulator
requirements below apply to both interfaces. To enable the pinned simulator in
Instrument, use the server-side launcher option:

```sh
uv run --locked uav-debugger-instrument \
  --experiment-root local/experiments \
  --sitl-binary "$PWD/local/sitl/arducopter"
```

The browser cannot submit an executable path, command, arbitrary output path or
PID. Selecting SITL retains pinned-binary validation and the mandatory isolated
worker path, with no fallback to the host network.

## Launch and run

The following launch procedure describes the existing Streamlit interface.
From the repository root:

```sh
uv sync --locked
uv run --locked uav-debugger-analyze
```

Open [http://127.0.0.1:8501](http://127.0.0.1:8501). The application starts in
**Analyze**. Choose **Experiment** under **Mode** to configure execution.
Changing modes or settings does not start a run.

1. Choose **Synthetic** under **Experiment source** and **Baseline** under
   **Scenario**. Keep **Duration (s)** at six seconds.
2. Click **Start experiment**. The server creates a new run directory and starts
   one worker for the synthetic sender → relay → receiver path on loopback UDP.
3. Watch **Process state** and **Controller elapsed**. These describe execution;
   they are not live telemetry measurements. Wait for the worker to finish, or
   click **Stop experiment** to request orderly interruption.
4. Select the terminal run under **Run**. Review its **Declared outcome** and
   any worker diagnostics, then click **Open in Analyze**.
5. Analyze reads the retained files, checks their evidence, and opens the
   [saved-run view](saved-experiments.md). Inspect both observation points,
   filters and original records, then download the report.

For a blackout, choose **Blackout** and set **Blackout start (s)**, which defaults
to two seconds after measurement start. **Blackout duration (s)** defaults to
two seconds and accepts 0.1–59.8 seconds. Keep at least 0.1 second before and
after its requested interval within **Duration (s)**, which accepts 0.1–60
seconds. For example, duration 3, blackout start 0.4 and blackout duration 0.5
request a half-second interruption. Invalid combinations are rejected before
creating a run; no setting is silently shortened to fit. Blackout controls are
disabled for a baseline and all execution settings are disabled during a run.

Requested timing is distinct from the actual gate transitions recorded by the
runner. **Stop experiment** can interrupt an active gate. The
[Experiment contract](experiment.md) defines scheduling, captures and clocks.

The launcher supports another port or an explicit server-side output root:

```sh
uv run --locked uav-debugger-analyze \
  --port 8502 --experiment-root local/browser-experiments
```

Paths resolve on the machine running the server. With
[SSH forwarding](analyze.md#access-through-ssh), experiments run on that machine;
**Open in Analyze** reads the server's retained evidence without a browser upload.
The listener remains `127.0.0.1`, and usage statistics remain disabled.

## Compare a baseline and blackout

1. Complete a baseline and click **Use as baseline** for that run.
2. Start a blackout with the same source, duration and profile. When it finishes,
   click **Use as blackout**. The **Run** selector also lets you choose an earlier
   retained run and assign its corresponding role.
3. Check the displayed run identifiers and click **Compare selected runs**.
4. Analyze validates both evidence sets and opens
   [Compare experiments](comparison.md). Review compatibility, the explicit
   measurement-relative window, observed metrics and the downloadable report.

Assignment does not assert successful completion or compatible evidence.
Interrupted, incomplete or incompatible runs remain inspectable and produce
blocked comparisons with reasons. Counts, rates and intervals come from saved
observations; they are not inferred from the requested transmission rate.

## Shared execution and shutdown

One worker can run at a time **per application server**. All tabs connected to
that server share its active run and Stop control. The controller rejects a
second launch even if another tab shows an older idle view. Analysis selections
and assigned comparison roles remain specific to each browser session.
Separately launched Instrument and Streamlit servers have independent
controllers; starting both does not create a shared execution coordinator.

Switching to Analyze, reloading or closing a browser tab lets the bounded run
continue. Reopening Experiment shows the current server state; it does not
restart a run. The latest 20 runs are retained in server memory. Older output
directories remain on disk and are never deleted automatically. Restarting the
server creates an empty history without discovering or executing old runs;
use **Browse saved experiments** to open the separate
[Local experiments catalog](saved-experiments.md#browse-local-experiments) in
Analyze. It reads retained directories under the configured output root,
including CLI runs. Browsing does not restore controller history or live process
state. Opening and comparison validate the selected saved evidence.

Stopping the server normally requests worker cleanup and waits for its exit.
The worker also watches its ownership pipe: if the server exits abruptly, EOF
requests the runner's ordinary interruption and evidence finalization. An abrupt
server exit can leave `control.json` with its previous controller state even
when the worker finalizes `run.json`; neither file proves a process is currently
alive.

While the server is alive, a watchdog allows the requested measurement duration,
the configured SITL startup allowance when applicable, and eight additional
seconds for launch and cleanup. Exceeding that allowance requests Stop. A worker
that remains active five seconds after a stop request is forcibly terminated;
the controller records failure and the forced termination. These bounds are
not hard real-time guarantees under suspension, blocked storage or operating
system failure. Forced termination, crashes and storage failures can leave
partial evidence; they never establish successful completion.

## Output, states and clocks

The default root is `local/experiments`, created only by an accepted Start.
Each run uses a fresh directory:

```text
local/experiments/run-<identifier>/
  control.json
  evidence/
    run.json
    actions.jsonl
    observations.jsonl
    relay-input.tlog
    receiver.tlog
    ... optional SITL artifacts
```

`control.json` records the controller request, worker state, exit status,
stop reason, forced termination and a diagnostic tail of at most 8,192 worker
stderr bytes. There is no separate worker log. The `evidence` directory retains
the runner's existing files and schema; SITL additionally records its own
`simulator.log`, datagram trace and parameter profile.

| Value | Meaning |
| --- | --- |
| **Controller state** in Instrument / **Process state** in the existing interface | Controller lifecycle: `idle` before a run; `starting`, `running`, `stopping`, then `finished` or `failed`. It does not establish simulator readiness or evidence consistency. |
| **Declared outcome** | The terminal worker's saved runner outcome, such as `completed`, `interrupted` or `failed`. An unfinalized manifest can still say `running`; an absent or unreadable outcome is `N/A` in Instrument or `Unavailable` in the existing interface. |
| **Controller elapsed** | Monotonic elapsed time since the Start request, including launch and cleanup. It is not the requested or measured telemetry duration. |
| **Evidence status** in Analyze | The saved reader's `consistent`, `incomplete` or `invalid` result from hashes, captures and references. It is independent of both process state and declared outcome. |

Instrument's **Controller clocks and diagnostics**, or **Controller requests
and clocks** in the existing interface, retain host `monotonic_ns` and separately
sampled Unix `unix_us` for Start, Stop and worker completion. A Stop request time
does not substitute for the runner's `producer_stopped` action. Runner actions,
observation timestamps, capture wall clocks and payload clocks keep their
original meanings. The active view reads controller snapshots. Its Analyze and
comparison handoffs read saved captures only after worker termination.

Keep the entire run directory when retaining execution evidence. The offline
reader accepts the fixed files inside `evidence`; it does not merge the outer
controller requests into runner actions or include `control.json` in the saved
run fingerprint/report. Original capture files remain usable independently.

## Enable the pinned simulator

First obtain and verify the exact executable documented in the
[ArduCopter SITL guide](sitl.md#prepare-the-simulator). Then launch:

```sh
uv run --locked uav-debugger-analyze \
  --experiment-root local/browser-experiments \
  --sitl-binary "$PWD/local/sitl/arducopter"
```

Select **ArduCopter SITL**. **Startup timeout (s)** in Instrument, or **SITL
startup timeout (s)** in the existing interface, defaults to 15 seconds and
accepts 0.1–60. The runner begins measurement after receiving the required
HEARTBEAT and ATTITUDE from source `1 / 1`; startup observations are retained.
The configured executable is checked against the pinned hash before launch.
The interface does not download a simulator or install dependencies.

Linux `unshare`, `ip`, and permission to create user, network and PID namespaces
are required. Each SITL worker gets its own namespaces with only loopback
enabled; the browser server keeps its original namespace and stays reachable.
Failure to establish isolation fails the launch without falling back to the
host network. The worker is PID 1 in its PID namespace, so its termination also
removes remaining simulator processes. Simulator PIDs in the saved manifest
belong to that namespace and must not be interpreted as host PIDs. See
[unshare](https://man7.org/linux/man-pages/man1/unshare.1.html) and
[PID namespaces](https://man7.org/linux/man-pages/man7/pid_namespaces.7.html).

The supported profile sends no MAVLink commands and remains disarmed. It
establishes local telemetry observations, not flight behavior, failsafe response
or physical link performance. Analyze needs none of these execution prerequisites
when inspecting saved files.
