# Project scope

UAV Debugger is a planned standalone tool for understanding UAV sessions and
testing system behavior through controlled experiments. It has two modes:
**Analyze** and **Experiment**, built around the same session evidence.

This repository currently contains documentation only. The capabilities below
describe the intended product; no import format, application interface or
implementation stack has been selected or delivered.

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

Experiment is intended to let users configure and execute reproducible MAVLink
perturbations in an explicitly identified simulation or bench environment,
capture their application and inspect the results through Analyze.

An experiment should preserve its scenario, configuration, timing and execution
evidence so that another run can use the same conditions. Reproducing the
perturbation does not guarantee an identical vehicle response.

The experiment runner should expose enough information to understand what it
applied and whether the run completed. Detailed investigation belongs to the
shared analysis workflow, rather than a separate competing viewer.

## Product boundaries

- The initial vehicle scope is UAVs.
- The product focuses on observation, investigation and controlled experiments;
  it is not a general flight-control station or an autonomy system.
- Experiment initially concerns simulation and bench work. Live-flight fault
  injection is outside the current scope.
- ARGOS is a possible integration case, not a runtime dependency. Importers and
  experiment connections should allow use by other UAV projects.

## Evidence and open choices

Planned perturbations, applied perturbations and observed outcomes must remain
distinguishable. Receiving a message does not prove that an action executed;
adjacent events on a timeline do not by themselves establish causation.

Supported inputs, clock alignment, the initial interface, deployment model and
implementation stack are open. See [architecture](architecture.md) for design
boundaries and [the first milestone](first-milestone.md) for a proposed starting
point. Return to the [project overview](../README.md).
