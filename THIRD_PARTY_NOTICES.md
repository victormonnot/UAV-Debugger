# Third-party notices

UAV Debugger uses the following pinned runtime dependencies. They are installed
separately; the UAV Debugger wheel contains its own Python modules, synthetic
recording and the separately listed Instrument browser assets. It does not vendor
copies of these Python libraries. Each dependency retains
its own copyright notices and license terms.

| Dependency | Version | Upstream licensing information |
| --- | --- | --- |
| pymavlink | 2.4.49 | LGPL version 3, with an MIT exception for generator output; the imported CRC module declares LGPL version 3 or later. |
| Streamlit | 1.63.0 | Apache License 2.0. |
| Plotly | 7.0.0 | MIT License. |
| Starlette | 1.7.0 | BSD 3-Clause License. |
| Uvicorn | 0.53.0 | BSD 3-Clause License. |

## pymavlink

See the versioned [COPYING](https://github.com/ArduPilot/pymavlink/blob/v2.4.49/COPYING)
and [CRC module header](https://github.com/ArduPilot/pymavlink/blob/v2.4.49/generator/mavcrc.py).
The upstream distinction between the library and generated output matters:
UAV Debugger imports both generated `common` definitions and
`pymavlink.generator.mavcrc.x25crc`. The dependency as used here must not be
described as entirely MIT-licensed.

## Streamlit

See the versioned [Apache 2.0 license text](https://github.com/streamlit/streamlit/blob/1.63.0/LICENSE).
Streamlit provides the full analysis and experiment interface and its supporting
components.

## Plotly

See the versioned [MIT license text](https://github.com/plotly/plotly.py/blob/v7.0.0/LICENSE.txt).
Plotly provides the activity and attitude charts. Instrument serves the installed
Plotly package's JavaScript bundle locally; it does not download a CDN copy.

## Starlette and Uvicorn

See Starlette's versioned [license text](https://github.com/Kludex/starlette/blob/1.7.0/LICENSE.md)
and Uvicorn's versioned [license text](https://github.com/Kludex/uvicorn/blob/0.53.0/LICENSE.md).
Starlette provides Instrument's local HTTP routes and static-file responses;
Uvicorn runs that application on loopback.

## Bundled Instrument browser assets

The following files under `uav_debugger/instrument_static/vendor/` are copied
unchanged from the named official npm distributions. Only the listed Latin,
normal-style WOFF2 font files and the minified Lucide UMD bundle are included.
The package license texts are retained alongside them, under the local names
below. No npm installation or external font/icon request is needed at runtime.

| Package | Version and source archive | Included files | License text |
| --- | --- | --- | --- |
| `@fontsource/ibm-plex-sans` | [5.3.0](https://registry.npmjs.org/@fontsource/ibm-plex-sans/-/ibm-plex-sans-5.3.0.tgz) | `files/ibm-plex-sans-latin-{400,500,600}-normal.woff2` | `ibm-plex-sans-LICENSE.txt`, SIL Open Font License 1.1, IBM copyright. |
| `@fontsource/ibm-plex-mono` | [5.3.0](https://registry.npmjs.org/@fontsource/ibm-plex-mono/-/ibm-plex-mono-5.3.0.tgz) | `files/ibm-plex-mono-latin-{400,500}-normal.woff2` | `ibm-plex-mono-LICENSE.txt`, SIL Open Font License 1.1, IBM copyright. |
| `@fontsource/marcellus` | [5.3.0](https://registry.npmjs.org/@fontsource/marcellus/-/marcellus-5.3.0.tgz) | `files/marcellus-latin-400-normal.woff2` | `marcellus-LICENSE.txt`, SIL Open Font License 1.1, Brian J. Bonislawsky DBA Astigmatic copyright. |
| `lucide` | [1.52.0](https://registry.npmjs.org/lucide/-/lucide-1.52.0.tgz) | `dist/umd/lucide.min.js` | `lucide-LICENSE.txt`, ISC; includes the MIT notice for icons derived from Feather. |

Fontsource's upstream font collection is
[fontsource/font-files](https://github.com/fontsource/font-files), under
`fonts/google/ibm-plex-sans`, `fonts/google/ibm-plex-mono` and
`fonts/google/marcellus`. Lucide's upstream is
[lucide-icons/lucide](https://github.com/lucide-icons/lucide), under `packages/lucide`.
The pinned archives above identify the exact distributed assets independently of
subsequent changes to those repositories.

SHA-256 fingerprints of the bundled files:

| Local filename | SHA-256 |
| --- | --- |
| `ibm-plex-sans-latin-400-normal.woff2` | `3b646991d30055a93a4ecc499713d4347953a74a947ecab435ab72070cbdab0e` |
| `ibm-plex-sans-latin-500-normal.woff2` | `0717336fb31fcdcde4b8deb3675bb4a0f7f6d484864afcd6751ac29975962203` |
| `ibm-plex-sans-latin-600-normal.woff2` | `8960851d691c054ed38e259bdcf1a6190d157b4203ed5bb32c632a863fb8ec2f` |
| `ibm-plex-mono-latin-400-normal.woff2` | `08949f728dc52d528e69b1667d15c89a5686a4ee9a296ff90983985f99c380f7` |
| `ibm-plex-mono-latin-500-normal.woff2` | `01d285447409c8a588692162439a038b8cbd7871309ee20267b0d2d91c6e8e22` |
| `marcellus-latin-400-normal.woff2` | `8a539799d12e3a144273288055490f57e1eee84da7a9145f085bc522e80719c3` |
| `lucide.min.js` | `f828bf34002c03a37da4acecfd0cf214935c4c6c26d4bc48903457ba6c44e606` |
| `ibm-plex-sans-LICENSE.txt` | `d0283623ef57e722fd0eb688a8041589670c608ab780cd3612d06ba6f153d3fd` |
| `ibm-plex-mono-LICENSE.txt` | `23b0a9d0c6d3f140a0b77e483c5cfa6bba574325ef5cb189ed9f2fec4884533f` |
| `marcellus-LICENSE.txt` | `a015677ab0010a8c14729226613516401d56e7c0f929ae54f1549eada2bab12c` |
| `lucide-LICENSE.txt` | `b495047bd93a9b06913511076f504daba17d5bbeb3e0650f3bb53a4220329c57` |

## Scope

This notice records the direct runtime dependencies declared in `pyproject.toml`
and the vendored Instrument assets above.
It is not an exhaustive inventory of transitive dependencies, components bundled
by those dependencies, or development/browser tooling. Consult their own
distributions and notices when redistributing an environment or a bundled
application. `uv.lock` records the resolved dependency versions. The MIT license
for UAV Debugger's original code, documentation and synthetic fixture does not
replace the terms of those dependencies or browser assets.

The separately obtained public QGroundControl recording is not a project fixture
or a distributed dependency. Its provenance and verification limits are recorded
in [the public recording check](https://github.com/victormonnot/UAV-Debugger/blob/main/docs/recording-validation.md); it is not included
in UAV Debugger packages.
