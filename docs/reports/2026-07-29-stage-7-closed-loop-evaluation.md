# Stage 7 — First Offline Closed-Loop Evaluation

Date: 2026-07-29

## Result

The shot-level loop was rebuilt against the existing real Kling `s01` MP4,
the current persisted Stage 4 camera report, and a manual inspection record
bound to the exact first/middle/last frame hashes. Rebuilding this evidence did
not call a backend API or run model inference.

At the lowered default phase-correlation gate (`1.05`), the automatic
background-translation heuristic reports `matched`. The preserved strict
reference gate (`1.25`) remains `inconclusive`. This is heuristic evidence,
not a camera-pose measurement and not proof that camera control improved.

The manual inspection records that actor A approaches from frame left, actor B
remains standing on frame right, both face one another, and the last frame
shows a handshake. These observations remain labelled
`manual_visual_inspection`; they are not reported as detector output.

## Hash-linked records

- `expectation.json`: `2eeb8db9116719f538a1d309f5d964c5ebd648bd778d80bb3c03348f64d376b4`
- `feedback.json`: `b36d187941e8109d2b1ee6da3eae4e9508d114b4194c9eee27fe405081a36939`
- `revision.json`: `5e7a8f17647681c41aa3ea3c6e2b461496b877ac38bd01808902610effe6e3c7`
- current Stage 4 report: `f3b0e1d41e03affc04334b9c6c891b3f04ced8aea8951d09f626a88a1403c390`

The lowered `1.05` result is retained as `heuristic_verdict`, but it is not the
closed-loop acceptance signal. The fixed revision policy consumes the strict
`1.25` reference verdict, which remains `inconclusive`, and therefore emits
exactly two bounded operations: enable the structural VACE source-video
channel and preserve the ShotScript camera trajectory. All manual action checks
remain matched.

## Verification

- Stage 4 focused suite: 13/13 passed.
- Stage 7 focused suite: 10/10 passed.
- Real Kling MP4 SHA-256:
  `f3a95d76e898858726be8651f0472bafd0ff6a33e6c0e6743f5e23730aae35f3`.
