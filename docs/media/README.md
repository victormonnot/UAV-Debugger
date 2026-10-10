# README screenshots

These images show the actual Instrument interface from source revision
`301748b5c11df8297d339216ca51eec3f6c6aaed`, captured on October 10, 2026.
They were taken in Chromium at 1440 × 960 with the dark theme, without changing
the interface or plotted data for the screenshots.

- `instrument-analyze.png` shows the receiver's attitude measurements from a
  six-second synthetic experiment with a requested two-second forwarding
  interruption beginning two seconds into the measurement period.
- `instrument-comparison.png` shows that experiment compared with a separate
  six-second synthetic baseline. Both were started through Instrument's
  Experiment controls and reopened through its normal analysis workflow.

The source sends artificial attitude signals over loopback UDP. These are
software experiments, not physical-flight recordings or a flight-dynamics
simulation. Timing and counts are observations from the recorded run, not
promises of identical scheduling on another machine. No user recordings or
external photographs are included.

The screenshots are covered by the repository's [MIT License](../../LICENSE).
Bundled interface fonts, icons and chart assets retain the terms listed in
[THIRD_PARTY_NOTICES.md](../../THIRD_PARTY_NOTICES.md).
