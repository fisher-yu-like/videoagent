# Stage 3 Addendum — Offline Backend Evidence Audit

Date: 2026-07-29

The Stage 3 verifier audits the existing persisted Kling run without making a
network request. Its evidence level is deliberately limited to
`persisted_gateway_records_internal_consistency`: the local records agree with
one another, but the run is not anchored to an external immutable record.

## Checks performed

- The submission is a POST to the HTTPS host `modelservice.jdcloud.com`, and
  its declared model belongs to the Kling model family.
- The response, every query, and final state contain the same task ID.
- The last query and state both report `success`.
- The last query's returned video URLs exactly match the URLs in final state.
- The download record's byte count and SHA-256 match the local MP4.
- Blender can read the MP4 and report its media metadata.
- Every JSON file read by the audit is bound into `evidence_files` with its
  run-relative path, exact raw-byte length, and SHA-256. For this run that is
  `metadata.json`, `request.json`, `response.json`, `state.json`,
  `download.json`, and all four `query_*.json` records.

The verifier does not claim that the persisted JSON records independently prove
what the remote service returned. It does not interpret billing or assign a
quality score.

## Existing Kling artifact

- Task ID: `task-ebzlp2kd3xv3tqy`
- Status sequence: `pending → running → running → success`
- Result size: 7,207,519 bytes
- Result SHA-256: `f3a95d76e898858726be8651f0472bafd0ff6a33e6c0e6743f5e23730aae35f3`
- Blender 5.1.2 metadata: 1280×720, 121 frames, 24 fps,
  5.041666666666667 seconds

The Seedance outcome remains `unknown`. Seedance information is labelled
`project_context_not_part_of_run`; no timeout reason or retry claim is inferred
from this Kling run.

## Output and offline guarantees

An audit may be written only to the reserved `<run-dir>/audit.json` path or to a
path outside the run directory. The write uses a sibling temporary file, flushes
it, and atomically replaces the target. A failed replacement preserves the
previous audit and removes the temporary file.

The current `runs/stage3_api/20260729T012120Z_kling_d8bde2ef/audit.json`
SHA-256 is
`dcafd6ec04ec8dbc8077f213183e2be4cb81c21acdb7dc8ea5491495e46a5e69`.
The evidence-file records are calculated from the same raw bytes used for JSON
parsing, so the audit cannot accidentally summarize one read while hashing a
later read of the file.

The offline regression test disables Python socket construction, socket
connections, and `urllib.request.urlopen` while auditing the real MP4. Blender
is still invoked locally as a subprocess for media probing. No API request is
made by the audit.

## Verification

The focused Stage 3 audit suite passes 11/11 tests. These include real MP4 and
Blender verification, negative evidence-mutation tests, output-collision
protection, atomic-write failure injection, raw-byte provenance binding for all
JSON evidence, and the network-denial test.
