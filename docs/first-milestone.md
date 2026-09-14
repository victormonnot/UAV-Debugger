# First milestone proposal

**Status: proposed release scope; not implemented.** This proposal covers one
offline Analyze workflow, followed by a possible Experiment release using the
same session analysis. The product scope includes both modes.

## Candidate: inspect one MAVLink recording

An engineer opens a supported recording without connecting to a vehicle. They
can identify its sources, inspect message contents and timing, navigate an
interval and see any import limitations. The result must work without ARGOS,
a simulator, external network access during analysis or an experiment runner.

The first implementation should select one documented external recording format
and an explicit MAVLink dialect. A timestamped MAVLink recording is a candidate;
its actual container and clock convention must be specified rather than inferred
from a filename. The supplied example must be usable independently of ARGOS.

## Included in the proposed slice

1. A small redistributable fixture with documented generation or provenance,
   known source identities and independently specified expected observations.
2. Input validation with visible complete, unsupported and damaged-input outcomes.
3. A session summary, message inspection and source/time filtering with clear
   timestamp units and origin.
4. A reproducible launch workflow and concise user guide for the chosen interface.
5. An inspectable report or saved analysis result identifying input provenance
   and limitations.

Video synchronization, multi-format import, cross-session comparison and active
experiments can follow as separate deliveries. Their absence in this proposed
first slice must remain clear in the interface and documentation.

## Acceptance criteria

- A fresh documented installation can open the supplied fixture offline.
- Two source identities in the fixture remain distinguishable after filtering.
- Expected decoded values and chronology agree with independent fixture evidence;
  damaged/truncated input has an explicit outcome rather than an invented value.
- Missing or ambiguous clock information remains visible. The report does not
  present unmeasured sensor latency or physical packet loss as established facts.
- Original recordings remain byte-for-byte unchanged; generated outputs have a
  separate destination and identify the source they describe.
- No vehicle connection, simulator startup, injection or packet transmission is
  triggered by loading or navigating the recording.
- Validation covers the actual importer and user workflow, with results recorded
  for the delivered version. No test result exists for this proposal yet.

## Candidate follow-up: one controlled experiment

After a usable analyzer exists, a separate delivery could forward a known local
MAVLink stream through a bounded experiment path, introduce one specified
one-direction interruption, record applied actions and endpoint observations,
and inspect the resulting session in Analyze. Nominal and perturbed runs should
share the same configuration except for the studied change.

That delivery needs its own target topology, scenario semantics, recording
format, stop behavior and verification criteria. A stream experiment would
establish behavior on that configured path; autopilot response would require an
additional actual simulator/bench integration and its own evidence.

See [project scope](project-scope.md) and [architecture direction](architecture.md)
for the product boundaries and shared analysis design.
