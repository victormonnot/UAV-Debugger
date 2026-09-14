# Workflows

The current [Analyze interface](analyze.md) covers opening one supported file,
filtering observations, inspecting records and exporting evidence. The broader
workflows below also describe future capabilities: video, session comparison
and active Experiment. The intended modes share session analysis, with either
usable independently of the other.

## Analyze an existing session

1. Select recordings in a supported format and identify their sources.
2. Review what was imported, what is missing and any parsing limitations.
3. Inspect the timeline, telemetry, events and available video.
4. Follow a question from an event to its underlying source evidence.
5. Compare another session when useful, preserving differences in conditions.
6. Save findings with evidence references and unresolved questions.

For example, a user investigating a telemetry gap could inspect the last
available messages, any reported mode change and a corresponding video segment.
If the recording contains no evidence of the vehicle during the gap, the tool
should show that absence rather than infer what the UAV did.

Sources may have different clocks or incomplete timestamps. An aligned view
should retain original timestamps and disclose its alignment assumptions.
When reliable alignment is unavailable, that uncertainty remains visible.

## Run a controlled experiment

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

The session should retain the scenario and relevant environment configuration,
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

An independently imported session and a session produced by Experiment should
use the same analysis concepts. The current recording representation is concrete;
the future experiment trace remains to be defined. See [architecture](architecture.md)
and the [first milestone](first-milestone.md) for the implementation boundary.
