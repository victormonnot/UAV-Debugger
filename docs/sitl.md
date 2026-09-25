# Local ArduCopter SITL experiment

The unreleased Experiment CLI supports one pinned ArduCopter SITL profile on
Linux x86_64. The autopilot runs as an owned local subprocess and emits telemetry
through the same relay and receiver used for synthetic experiments. The runner
sends no MAVLink commands to the autopilot and does not arm or fly it.

This profile verifies telemetry forwarding and observation. It does not test
an autopilot failsafe or establish vehicle behavior during a receiver-side gap.
Analyze remains independently usable without installing the simulator.

## Prepare the simulator

Install the project normally with `uv sync --locked`. Download this specific
external executable from the official firmware archive and verify its bytes:

```sh
mkdir -p local/sitl
curl -fL --output local/sitl/arducopter \
  https://firmware.ardupilot.org/Copter/stable-4.6.3/SITL_x86_64_linux_gnu/arducopter
printf '%s  %s\n' \
  7862662092edc2861fc03da3d6fb2f0136d1670e563ca324eb52c1a324d1e14b \
  local/sitl/arducopter | sha256sum --check
chmod +x local/sitl/arducopter
```

The verified artifact is ArduCopter **4.6.3**, 7,023,152 bytes, from upstream
commit `3fc7011a7d3dc047cbb17d8bd98ee94577d144c6`. The runtime checks this exact
SHA-256 before executing it; another build requires separate integration
verification. A matching checksum identifies the tested artifact, not a digital
signature. The binary is downloaded separately and is not bundled in project
packages; ArduPilot retains its [own license](https://github.com/ArduPilot/ardupilot/blob/3fc7011a7d3dc047cbb17d8bd98ee94577d144c6/COPYING.txt).

The profile uses the built-in quad model, speedup 1, fixed startup parameters,
MAVLink on serial 1 and disabled unused serial transports. Every run gets a new
simulator working directory and parameter file, so earlier `eeprom.bin` state
cannot silently alter the next run. The manifest retains the executable
identity, exact arguments and requested startup parameters. These are setup
inputs, not a readback of every effective autopilot parameter.

## Run with only loopback networking

ArduCopter's RC-input backend binds a UDP port on all interfaces even when
telemetry uses loopback. This profile therefore **requires a Linux network
namespace containing only the loopback interface**. It refuses to launch in an
ordinary host namespace with other interfaces. Namespace support and the `ip`
command are prerequisites; failure to isolate networking is an error.

Run the baseline from the repository root:

```sh
unshare --user --map-root-user --net sh -c '
  set -eu
  ip link set dev lo up
  exec "$@"
' sh "$PWD/.venv/bin/uav-debugger-experiment" \
  --sitl-binary "$PWD/local/sitl/arducopter" \
  --output local/experiments/sitl-baseline --scenario baseline
```

Then repeat the command with `--output local/experiments/sitl-blackout` and
`--scenario blackout`. Each output directory must be new. These commands create
an isolated namespace for the runner and its simulator; only their local
interfaces are available. They do not change the host's network configuration.

The runner waits at most `--startup-timeout` seconds (default 15, range 0.1–60)
for valid `HEARTBEAT` and `ATTITUDE` messages from source `1 / 1`. Their receipt
establishes readiness for this telemetry experiment, not flight readiness.
Then the configured `--duration` begins (default six seconds). The blackout
requests activation two seconds after that measurement start and lasts two
seconds from actual gate activation. Existing duration and blackout constraints
from the [Experiment guide](experiment.md) apply.

Startup observations remain in the captures. `measurement_start` in `run.json`
and `measurement_started` in the action trace identify the start of the measured
phase; the run's original monotonic origin remains unchanged. A readiness timeout
or premature simulator exit makes the run fail and retains available evidence.
Actions after readiness also include `measurement_elapsed_ns`, relative to that
measurement origin. The gate's `requested_elapsed_ns` instead locates its
requested deadline relative to the original run origin, including startup.

`Ctrl+C` and `SIGTERM` request orderly shutdown. The runner terminates only its
owned simulator process group, waits up to two seconds, then escalates to kill
with a further one-second wait if needed. It drains local datagrams for at most
0.2 seconds and closes resources. These deadlines exclude arbitrary process
suspension and blocked filesystem I/O. The shutdown result is recorded.

## Read the evidence

The existing `run.json`, `actions.jsonl`, `observations.jsonl`,
`relay-input.tlog` and `receiver.tlog` retain their distinct roles. This source
uses manifest schema `uav-debugger-experiment-v2` and additionally records:

- `datagrams.jsonl`: each actual UDP read at either observation point, including
  its original bytes as hex, datagram index and host timestamps.
- `simulator.log`: stdout/stderr from the owned autopilot process.
- `simulator/profile.parm`: the exact requested startup parameter file.

Each UDP datagram can contain several complete unsigned MAVLink 1/2 frames.
The relay forwards or drops the entire datagram without changing it. Each frame
gets its own Analyze record with the same receipt timestamp for that datagram;
repeated-timestamp warnings are therefore possible. Observation entries include
`datagram_index` and `datagram_offset`, alongside the existing capture record and
byte references. Action counters count MAVLink frames; datagram actions/counts
are separate. An external sender's submission count is unknown and stored as
`null`, not inferred from relay observations.

The pinned firmware sends the exact bytes `30 20` three times while initializing
its MAVLink UART to exit a SiK radio bootloader. At each point, the runner accepts
at most three such datagrams before the first MAVLink frame. It retains and
forwards their bytes and records `sitl_startup_preamble` actions; it does not
invent MAVLink records for them. Other non-frame bytes, later/extra preambles,
signed frames, unsupported flags, partial frames or invalid known-message
checksums fail the run. The rejected datagram remains in the raw trace. There
is no resynchronization or frame reassembly across datagrams.

Decoding still uses the existing `common` dialect. ArduPilot-specific messages
outside it stay opaque, with unverified checksums, according to the
[importer contract](importer.md). Complete traversal and complete decoding remain
different outcomes. Capture size is bounded by Analyze's 10 MiB limit; raw
JSONL datagram evidence is capped around 32 MiB. Reaching a cap fails the run
with the retained prefix.

Host monotonic time drives scheduling; actual socket-read wall time supplies
outer Unix-microsecond timestamps. Payload `time_boot_ms` comes from the
simulated autopilot clock. No alignment with the host or reset at measurement
start is inferred. The loopback peer is selected from the first received
source datagram; this is a bounded local setup, not authenticated transport.

Open each capture independently in Analyze, choose source `1 / 1` and
`ATTITUDE`, inspect records and export reports. The first observations may be
from startup, so Analyze's relative time origin can precede `measurement_start`.
Choose **Saved experiment** to [inspect the complete run](saved-experiments.md),
including startup, measurement origin, applied gate intervals and both capture
points. No automatic cross-run comparison or active Experiment browser controls
are added.

## References and verification

The upstream [SITL overview](https://ardupilot.org/dev/docs/sitl-simulator-software-in-the-loop.html)
and [SITL usage guide](https://ardupilot.org/dev/docs/using-sitl-for-ardupilot-testing.html)
describe local autopilot execution and persistent parameter state. The pinned
[GCS initialization](https://github.com/ArduPilot/ardupilot/blob/3fc7011a7d3dc047cbb17d8bd98ee94577d144c6/libraries/GCS_MAVLink/GCS_Common.cpp)
and [RC UDP backend](https://github.com/ArduPilot/ardupilot/blob/3fc7011a7d3dc047cbb17d8bd98ee94577d144c6/libraries/AP_RCProtocol/AP_RCProtocol_UDP.cpp)
explain the explicit preamble handling and network isolation requirement.

Ordinary tests need no simulator download. Native integration checks are opt-in
with `UAV_DEBUGGER_SITL_BINARY` pointing to the pinned executable, inside the same
loopback-only namespace. The [verification guide](verification.md) records
actual local checks and the separate hosted CI status.
