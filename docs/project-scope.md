# Project scope

UAV Debugger is a standalone tool for understanding UAV recordings and running
controlled protocol experiments. It has two modes:
**Analyze** and **Experiment**, built around the same session evidence.

The unpublished **0.2.0rc2.dev0** development version provides an offline Analyze interface with
source, message and time filters, activity and attitude plots, record inspection
and Markdown evidence export. The same importer is available through Python and
a JSON command-line summary. The [Experiment interface](experiment-ui.md) and
[CLI](experiment.md) add a bounded synthetic sender → relay → receiver path on
local UDP, with a baseline and a two-second interruption in forwarding. A pinned ArduCopter SITL profile
can replace the synthetic source inside a loopback-only network namespace. See the [Analyze guide](analyze.md)
and [importer guide](importer.md) for tested inputs and limits. Analyze also
supports a bounded [saved baseline/blackout comparison](comparison.md).
Explicit browser Start/Stop controls feed the same saved-run inspection and
comparison. Video, broader session comparison and additional simulator/bench
integrations remain future capabilities.

## Analyze

Analyze provides:

- Import of supported recordings with retained bytes, timestamps and source information.
- Source/type/time filtering, message activity, attitude plots and original frame inspection.
- Saved-run inspection of requested settings, applied actions and observed captures.
- Read-only browsing of local experiment directories, with full validation on opening.
- Bounded comparison of a baseline and blackout with compatible profiles and evidence.
- Reports with references to the evidence and its limitations.

Analyze must be usable with saved files alone, without ARGOS, a connected UAV,
a simulator or an active experiment. Video and additional external observations
remain future inputs; they are not required by the implemented analysis.

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

The synthetic runner uses one process and two loopback UDP legs. Browser
execution owns a separate worker, allows one active run per server and records
controller requests independently of runner actions. The optional
[SITL profile](sitl.md) runs one fingerprinted ArduCopter subprocess, with
bounded startup and owned-process cleanup. It preserves the requested configuration, actual application records
and captures of socket reads at the relay input and receiver. Its duration,
shutdown, clocks and outcomes are explicit. Each capture opens independently
in Analyze. The [saved-run view](saved-experiments.md) additionally checks JSON
trace references and presents requested, applied and observed evidence.
The comparison view checks a saved baseline and blackout from the same profile
on an explicit common window; broader cross-run analysis remains future work.

This establishes behavior on the supported local telemetry paths. Integrating an
additional simulation or bench target will need a separate bounded use case and its own
verification. Reusing settings cannot guarantee identical timing or outcomes.

## Product boundaries

- The initial vehicle scope is UAVs.
- The product focuses on observation, investigation and controlled experiments;
  it is not a general flight-control station or an autonomy system.
- Experiment currently executes the local synthetic path and one pinned
  ArduCopter SITL profile. Further target integrations concern simulation and bench work; live-flight fault
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
