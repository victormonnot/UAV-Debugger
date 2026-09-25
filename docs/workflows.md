# Workflows

The current [Analyze interface](analyze.md) covers opening one supported file,
filtering observations, inspecting records and exporting evidence. The
unreleased [Experiment CLI](experiment.md) runs a bounded local synthetic path
and produces captures for that same analysis. A pinned [ArduCopter SITL profile](sitl.md)
can supply telemetry after explicit local setup and isolation. Video, session
comparison and broader simulator/bench integrations remain future capabilities.

## Analyze an existing session

1. Open one recording in a supported format and identify its sources.
2. Review what was imported, what is missing and any parsing limitations.
3. Filter source, message type and time; inspect activity and attitude plots.
4. Follow a question from an event to its underlying source evidence.
5. Save a report with evidence references and observation limits.

For example, a user investigating a telemetry gap can inspect the last and next
available messages from a selected source. If the recording contains no
evidence of the vehicle during the gap, the tool shows that absence without
inferring what the UAV did. Correlating video or comparing sessions is a later
workflow.

Sources may have different clocks or incomplete timestamps. An aligned view
should retain original timestamps and disclose its alignment assumptions.
When reliable alignment is unavailable, that uncertainty remains visible.

## Run the local synthetic experiment

1. Run the CLI with `--scenario baseline` and a new output directory.
2. Run it again with `--scenario blackout` and a different output directory.
   Keep the same requested duration; the default is six seconds in both cases.
3. Inspect each run's `run.json` outcome, requested settings and actual endpoint
   topology. Read the applied gate transitions in `actions.jsonl`.
4. Open `relay-input.tlog` and `receiver.tlog` separately in Analyze. Review
   provenance, filter source `1 / 1` and `ATTITUDE`, inspect the curves and the
   records around any interval, then save each report.
5. Follow capture references in `observations.jsonl` to relate actual reads to
   the run's monotonic and wall-clock evidence. Retain the full run directory.

The blackout suppresses relay forwarding for two seconds from actual gate
activation. The input capture keeps recording received datagrams; the receiver
capture contains only datagrams actually read downstream. Configuration alone
does not establish that a transition occurred, and send success alone does not
establish receiver observation. Exact counts and timing depend on scheduling.

Analyze opens each capture independently. Alternatively, choose **Saved experiment**
to [open the run directory](saved-experiments.md), check its evidence, inspect
applied actions alongside both capture points and export a combined report.
Cross-run alignment and automatic comparison remain future work. Opening any saved recording
never starts or resumes an experiment. The [Experiment guide](experiment.md)
defines the clocks, stop behavior, outcome codes and detailed usage.

## Run the pinned local simulator

Follow the [ArduCopter SITL guide](sitl.md) to install the exact executable and
run the same baseline/blackout scenarios inside a loopback-only namespace. The
runner records startup observations, waits for source `1 / 1` HEARTBEAT and
ATTITUDE, then begins the configured duration. Review readiness, measurement
start, applied gate transitions and simulator termination before opening the
captures. A telemetry forwarding gap does not establish an autopilot failsafe.

## Later simulation or bench workflows

1. State the behavior to investigate and the observations needed to assess it.
2. Identify the simulation or bench setup and the MAVLink path under test.
3. Configure the perturbation, affected traffic, trigger and duration.
4. Obtain or select a baseline when a comparison is needed.
5. Run the experiment and capture what the runner actually applies.
6. Open the resulting session in Analyze to inspect effects and compare runs.

For example, an experiment could interrupt one direction of telemetry for a
configured interval in simulation. The resulting session would distinguish the
requested interruption, the interval applied by the runner and the messages
seen at each recorded observation point. Any conclusion about vehicle behavior
would require the relevant vehicle evidence as well.

Such a session should retain the scenario and relevant environment configuration,
including any random seed when the perturbation depends on randomness. Failed,
cancelled and incomplete runs should remain distinguishable from completed runs.

## Read the evidence

| Evidence | What it establishes | What it does not establish alone |
| --- | --- | --- |
| Scenario configuration | What the experiment was intended to do | That the perturbation occurred |
| Runner application record | What the runner reports applying | The resulting vehicle behavior |
| Captured MAVLink message | What was seen at a recorded observation point | Delivery elsewhere or execution of a command |
| Vehicle state or external observation | What that source reports or shows | A complete history or the cause of a change |

Reports should preserve those distinctions and identify unavailable evidence.
The goal is to help assess hypotheses with traceable observations; automatic
root-cause identification is not an assumed capability.

## Compare and repeat

A comparison should make relevant differences in inputs, configuration and
available evidence visible. Reusing a scenario should reproduce its requested
conditions while retaining the actual application record for each run.

An independently imported recording and a capture produced by Experiment use
the same analysis components. The current CLI defines a concrete local trace;
broader target configuration and automatic comparison remain future work. See
[architecture](architecture.md) and the [first milestone](first-milestone.md)
for the implementation boundary.
