# UAV Debugger

A standalone tool for investigating UAV recordings and running controlled
experiments to understand system behavior.

**Status: design stage.** The product scope and two modes are documented;
no runnable application is available yet. The initial release scope is proposed
in the roadmap below.

## Two modes, one investigation workflow

| Mode | Purpose | Intended workflow |
| --- | --- | --- |
| **Analyze** | Explore logs, video and events. | Open supported recordings, inspect their sources and timing, examine an incident and compare sessions. |
| **Experiment** | Configure scenarios, run experiments and compare results. | Apply a specified MAVLink perturbation in an explicit simulation or bench setup, record what happened and examine the result in Analyze. |

The intended loop is to investigate an observation, formulate a hypothesis,
run a controlled experiment and compare the evidence. Experiment results and
ordinary recordings should use the same analysis tools.

Analyze is intended to work from saved files without a connected vehicle,
simulator, experiment service or ARGOS installation. Experiment adds an optional
active workflow; opening an archive must never start it.

## Intended scope

- UAV recording analysis with explicit source and time provenance.
- Navigation through telemetry, recorded events and, when supported, video.
- Comparisons that keep missing data, unsupported information and timing
  uncertainty visible.
- Reproducible MAVLink experiments on a configured simulation or bench target.
- Independent use with documented input formats; ARGOS is a possible integration.

These are intended capabilities, not a current compatibility list. No recording
format, dialect, media format, operating system or vehicle integration has been
implemented or verified in this repository.

## Read next

| Document | Contents |
| --- | --- |
| [Project scope](docs/project-scope.md) | Users, boundaries and product principles. |
| [Mode workflows](docs/workflows.md) | Analyze, Experiment and their shared results. |
| [Architecture direction](docs/architecture.md) | Proposed responsibilities and evidence conventions; technical choices still open. |
| [First milestone proposal](docs/first-milestone.md) | A bounded starting point and its acceptance criteria. |

Installation instructions and verified format support will accompany the first
runnable release.
