# Stage 4 addendum: relaxed gate with strict reference

Date: 2026-07-29

The default diagnostic gate is `1.05`, while every result also contains the
non-optional strict `1.25` reference. For the same real Kling frames, the
diagnostic result is `matched` and the strict result is `inconclusive`.

The machine-readable report is 3408 bytes with SHA-256
`f3b0e1d41e03affc04334b9c6c891b3f04ced8aea8951d09f626a88a1403c390`.
Stage 7 consumes the strict verdict and retains the relaxed result only as
`heuristic_verdict`. The focused suite passed 13/13 tests. No API, server, or
GPU was used while rebuilding this evidence.
