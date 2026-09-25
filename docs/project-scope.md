# Project scope

UAV Debugger is a standalone tool for understanding UAV recordings and running
controlled protocol experiments. It has two modes:
**Analyze** and **Experiment**, built around the same session evidence.

The current implementation provides an offline Analyze interface with source,
message and time filters, activity and attitude plots, record inspection and Markdown
evidence export. The same importer is available through Python and a JSON
command-line summary. The unreleased [Experiment CLI](experiment.md) adds a
bounded synthetic sender → relay → receiver path on local UDP, with a baseline
and a two-second interruption in forwarding. See the [Analyze guide](analyze.md)
and [importer guide](importer.md) for tested inputs and limits. Video, session
comparison, simulator integration and an Experiment interface remain future
capabilities.

## Analyze

Analyze is intended to help users:

- Import supported recordings and preserve their source information.
- Explore telemetry, events and available video along a timeline.
- Relate commands, reported vehicle state and external observations.
- Compare sessions, including a baseline and an experimental run.
- Record findings with references to the evidence and its limitations.

Analyze must be usable with saved files alone, without ARGOS, a connected UAV,
a simulator or an active experiment. Missing video or other optional sources
must not prevent analysis of the evidence that is available.

Compatibility will be defined by implemented and documented importers. The
project does not currently promise support for every UAV, autopilot or log
format. Video synchronization and particular media formats remain design work.

## Experiment

Experiment's broader direction is to configure and execute reproducible MAVLink
perturbations in an explicitly identified simulation or bench environment,
capture their application and inspect the results through Analyze.

An experiment should preserve its scenario, configuration, timing and execution
evidence so that another run can use the same conditions. Reproducing the
perturbation does not guarantee an identical vehicle response.

The first CLI uses one process and two loopback UDP legs, with no vehicle or
simulator. It preserves the requested configuration, actual application records
and captures of socket reads at the relay input and receiver. Its duration,
shutdown, clocks and outcomes are explicit. Each capture opens independently
in Analyze; JSON action traces and automatic comparison are not imported.

This establishes behavior on that synthetic local path. Integrating an actual
simulation or bench target will need a separate bounded use case and its own
verification. Reusing settings cannot guarantee identical timing or outcomes.

## Product boundaries

- The initial vehicle scope is UAVs.
- The product focuses on observation, investigation and controlled experiments;
  it is not a general flight-control station or an autonomy system.
- Experiment currently executes only the local synthetic path. Its intended
  target integrations concern simulation and bench work; live-flight fault
  injection is outside the current scope.
- ARGOS is a possible integration case, not a runtime dependency. Importers and
  experiment connections should allow use by other UAV projects.

## Evidence and open choices

Planned perturbations, applied perturbations and observed outcomes must remain
distinguishable. Receiving a message does not prove that an action executed;
adjacent events on a timeline do not by themselves establish causation.

The implementation uses Python and an explicit timestamped MAVLink profile.
The local experiment topology is documented; clock alignment across external
sources remains future work. See
[architecture](architecture.md) for implementation boundaries and
[the first milestone](first-milestone.md) for the delivered Analyze scope.
Return to the [project overview](../README.md).
