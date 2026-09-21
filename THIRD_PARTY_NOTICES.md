# Direct runtime dependencies

UAV Debugger uses the following pinned runtime dependencies. They are installed
separately; the UAV Debugger wheel contains its own Python modules and synthetic
recording, without vendored copies of these libraries. Each dependency retains
its own copyright notices and license terms.

| Dependency | Version | Upstream licensing information |
| --- | --- | --- |
| pymavlink | 2.4.49 | LGPL version 3, with an MIT exception for generator output; the imported CRC module declares LGPL version 3 or later. |
| Streamlit | 1.63.0 | Apache License 2.0. |
| Plotly | 7.0.0 | MIT License. |

## pymavlink

See the versioned [COPYING](https://github.com/ArduPilot/pymavlink/blob/v2.4.49/COPYING)
and [CRC module header](https://github.com/ArduPilot/pymavlink/blob/v2.4.49/generator/mavcrc.py).
The upstream distinction between the library and generated output matters:
UAV Debugger imports both generated `common` definitions and
`pymavlink.generator.mavcrc.x25crc`. The dependency as used here must not be
described as entirely MIT-licensed.

## Streamlit

See the versioned [Apache 2.0 license text](https://github.com/streamlit/streamlit/blob/1.63.0/LICENSE).
Streamlit provides the browser interface and its supporting components.

## Plotly

See the versioned [MIT license text](https://github.com/plotly/plotly.py/blob/v7.0.0/LICENSE.txt).
Plotly provides the activity and attitude charts.

## Scope

This notice records the direct runtime dependencies declared in `pyproject.toml`.
It is not an exhaustive inventory of transitive dependencies, components bundled
by those dependencies, or development/browser tooling. Consult their own
distributions and notices when redistributing an environment or a bundled
application. `uv.lock` records the resolved dependency versions; a project
license does not replace the terms of those dependencies.

The separately obtained public QGroundControl recording is not a project fixture
or a distributed dependency. Its provenance and verification limits are recorded
in [the public recording check](docs/recording-validation.md); it is not included
in UAV Debugger packages.
