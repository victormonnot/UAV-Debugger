# Compare saved experiments

The unreleased development version supports a bounded offline comparison of one
baseline and one blackout run in Analyze. Both runs must use the same synthetic
source or the same supported ArduCopter SITL profile. No simulator or running
experiment is needed to read their saved evidence.

## Open and compare

1. Produce a baseline and blackout with the [Experiment interface](experiment-ui.md),
   [CLI](experiment.md) or
   the optional [SITL profile](sitl.md), retaining each complete output directory.
   Use the same source settings; six seconds is the default measurement duration.
2. Start `uv run --locked uav-debugger-analyze`. Choose **Compare experiments**
   under **Analyze input** and select the baseline and blackout directories in
   their respective upload controls.
3. Review both fingerprints, declared outcomes, evidence status and configuration
   differences. Expand each run's settings and evidence to inspect file hashes
   and reader issues. Missing or incompatible evidence blocks aggregate results
   with explicit reasons; it does not become an empty, successful run.
4. Select **Comparison source** and **Comparison message type**. The initial
   choice is source `1 / 1`, `ATTITUDE` when available in both runs.
5. Review the common half-open window, initially zero through the shorter
   requested measurement duration. Edit **Window start (s)** and **Window end (s)**
   and choose **Apply comparison window** to narrow it. Decimal seconds accept
   up to nine fractional digits.
6. Inspect the table for both relay input and receiver, the differences and
   the activity plot. **Comparison observation point** switches the plot;
   tables and report retain both points. **Download report** exports the current
   applied comparison, including blocked results and their reasons.

In the Experiment interface, **Use as baseline** and **Use as blackout** assign
terminal runs from server history. **Compare selected runs** opens their saved
evidence in this same view without uploading files. Each role retains its
fingerprint, configuration checks and evidence limits. For later manual uploads,
select each browser run's `evidence` directory; the outer controller metadata is
separate from the runner's observations.

Each directory retains the [saved-run limits](saved-experiments.md#bounds-and-retained-references):
64 files / 64 MiB including auxiliary files, with 10 MiB per capture. The session
holds at most two selected runs; these byte limits do not bound total memory.
Unrecognized auxiliary files are listed and excluded from evidence identity.
**Clear comparison** releases both selections. Use **Clear baseline** or
**Clear blackout** before selecting a replacement directory; adding directories
together is rejected. Replacing a run resets comparison
controls; an unreadable replacement removes the previous report. Choose
**Saved experiment** to inspect either run's full capture records independently,
or **Recording** for ordinary file analysis.

## Clock and window contract

For each validated observation, the comparison uses:

```text
measurement-relative nanoseconds = observation.monotonic_ns - run.measurement_monotonic_ns
```

Each run keeps its own origin. Legacy synthetic v1 uses its recorded run origin;
SITL v2 uses the explicit measurement start after readiness. SITL startup and
post-measurement drain are excluded. Synthetic v1 includes the short setup
period between run origin and producer start in its requested measurement
interval; it is not silently shifted to the first emission. The common window
is `[start, end)`: a record exactly at the start is included and one exactly at
the end is excluded. Both requested durations must contain the window, and
validated production lifecycle evidence must reach its end.

Wall-clock capture timestamps, original frame bytes, indices and byte offsets
remain unchanged. No alignment is inferred from Unix time, the first record,
message sequence numbers or signal shape. Independent monotonic origins are
not treated as simultaneous events. The applied gate remains at its actual
recorded position relative to its own measurement start, even when the selected
window excludes it. Gate timing is never substituted with the requested timing.

## Eligibility and configuration

Comparison requires completed, consistent saved evidence with validated
observation references at both capture points, a measurement origin, production
lifecycle coverage and the expected baseline/blackout scenarios. A missing or
invalid artifact, ambiguous lifecycle, unavailable origin or incomplete gate
prevents aggregate comparison. The original runs and their issues remain
inspectable and exportable. The baseline must have no applied gate. The blackout
must request two seconds and contain one closed gate lasting at least two
seconds, no earlier than its requested deadline and within its measurement phase.

Synthetic and SITL sources cannot be mixed. Legacy v1 without an explicit source
field is interpreted as synthetic. For SITL, the declared executable profile and
SHA-256, exact parameter file identity and relevant launch arguments must match.
Reading saved metadata does not authenticate or execute the simulator binary.
The comparator ignores incidental output/executable paths and assigned UDP ports;
these do not define different requested telemetry behavior. Requested duration,
scenario, blackout timing and startup timeout differences are shown but do not
by themselves block a common-window comparison. Other behavior-setting changes
block it. Environment metadata differences remain visible without implying an
identical execution environment or repeatable scheduling.

## Observations and interpretation

Only validated sidecar references to imported records contribute. Source and
message type filters apply equally at both points in both runs.
Available selections have valid relay-input observations in both complete runs.
A selection may have no observations inside the narrower chosen window; an
identity or message type absent from either run blocks comparison instead.

| Measurement | Definition |
| --- | --- |
| Count | Selected observations within the explicit window. |
| Observed rate | Count divided by window duration in seconds. |
| Longest interval | Largest monotonic difference between consecutive selected observations within the window. |
| Difference | Blackout measurement minus baseline measurement at the same observation point. |

Zero counts and rates are available only for eligible, fully covered evidence.
Fewer than two selected observations give no longest interval, so its difference
is also unavailable unless both runs have an interval. The window edges do not
invent interval endpoints. First, last and longest-interval references identify
original capture indices and JSONL lines; they are not matched frames across runs.
Each point applies the window to its own observation timestamp. A frame observed
at relay input just before the window can be observed at the receiver just
inside it, or cross the ending boundary in the other direction. Receiver counts
can therefore exceed relay-input counts in the same window; that difference is
not a delivery ratio or a packet-loss estimate.

The chart counts all selected observations in up to 200 shared bins; it does not
resample attitude values. Shading uses actual validated gate intervals. The
report retains both bundle identities, file fingerprints, selected source/type,
window, origins, requested settings, configuration differences, applied actions,
observed metrics, reference limits and issues. Detail sections have explicit
size and count bounds with omission notices; keep the original directories.
The report includes at most 100 differences and 100 blocking reasons. Per run,
it includes at most 100 gate intervals, 100 lifecycle references and 100 reader
issues, and 20 observation excerpts per point and selection. Selected first, last and
longest-interval references are retained separately from those excerpts.
Each JSON section is limited to 65,536 characters, with an omission notice for
oversized content; source labels are capped at 4,096 characters and issue
messages at 256 characters. Aggregate metrics and reference excerpts occupy
separate sections so excerpt limits do not remove the measurements. Oversized
declared metadata has separate omission notices to preserve the file fingerprints.

An observed difference does not establish physical packet loss, an autopilot
response, a failsafe or a cause. This feature compares two recordings on a stated
relative window; it does not perform signal synchronization, automatic causal
diagnosis, repeated-run statistics or vehicle-response analysis. Explicit
launch/stop controls are available separately in [Experiment](experiment-ui.md);
opening or changing a comparison never executes either run.

## File-only Python API

```python
from uav_debugger.comparison import compare_runs
from uav_debugger.comparison_report import build_comparison_markdown_report
from uav_debugger.saved_run import load_run_directory

baseline = load_run_directory("local/experiments/baseline")
blackout = load_run_directory("local/experiments/blackout")
comparison = compare_runs(
    baseline,
    blackout,
    source=(1, 1),
    message_type="ATTITUDE",
    start_ns=0,
    end_ns=6_000_000_000,
)
print(build_comparison_markdown_report(comparison))
```

`comparable` identifies whether aggregate metrics are available. A blocked
result retains both runs and structured issues; `metrics` is empty. Invalid
argument types, out-of-range source IDs and malformed explicit windows raise
`ValueError`. A well-formed source/type that lacks evidence returns a blocked
result with `selection_unavailable`. None of these modules starts execution or
opens telemetry transports.
