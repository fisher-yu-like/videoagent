# Full-chain Seedance endpoint run (2026-08-10)

## Gate and input

本次没有绕过 Proxy gate。输入是历史真实 VLM 已批准的
`plaza_dance_circle` revision_005 Proxy；它被复制到新的不可变目录后才做后端提交：

`runs/results/e2e_seedance_approved_proxy_20260810_174500/`

Proxy gate：`approved`，确定性检查无 failed；四个参考视频来自同一个共享
WorldState 和同一个 render manifest。Appearance-only prompt 和 adapter bundle
沿用该已批准 run 的 hash-bound 文件，没有修改动作或相机指令。

## Seedance request

- Model: `Doubao-Seedance-2.0`
- Mode: reference-video
- Upload provider: Uguu（tmpfiles 首次上传真实失败，未重试同一 run）
- 每个 camera_id 一个独立 reference URL、一个独立 task；没有多视频 request、没有自动重试、没有换 seed。
- API 计数：`submit=4`、`query=84`、`download=4`。
- task IDs 和 request/response/query 日志位于 `seedance/camera_tasks/<camera_id>/`。

## Real downloaded videos

| Camera | Task | MP4 | Media |
|---|---|---|---|
| master | `task-o819flmyk1cbc06` | `seedance/camera_tasks/plaza_dance_circle_master/result.mp4` | 1280×720, 121 frames, 24fps, 5.086s, no black frames |
| lateral | `task-18wx9ehlez8fe7o` | `seedance/camera_tasks/plaza_dance_circle_lateral/result.mp4` | 1280×720, 121 frames, 24fps, 5.086s, no black frames |
| reverse | `task-hg6u2ep3l56gltv` | `seedance/camera_tasks/plaza_dance_circle_reverse/result.mp4` | 1280×720, 121 frames, 24fps, 5.086s, no black frames |
| elevated | `task-xpm2riobv7qzfr5` | `seedance/camera_tasks/plaza_dance_circle_elevated/result.mp4` | 1280×720, 121 frames, 24fps, 5.086s, no black frames |

SHA-256 and final media verifier reports are in
`final_video_verifier/<camera_id>.json` and `final_video_verifier_report.json`.

## Final visual audit

Final VLM call count: 1. Verdict: `revision_requested`.

The VLM found that the four independently generated outputs do not preserve the
shared world: master/lateral show one male dancer and plaza, while reverse and
elevated switch character identity, wardrobe, props and layout. This is a real
backend multi-view consistency failure, not a Proxy or media-integrity failure.
The result is therefore marked as downloaded media with a failed visual gate,
not as a successful multi-view generation. No second Seedance generation was
submitted.

## Separate park probe

`park_badminton` was tested with both canonical and skeleton proxies. Both real
VLM reviews requested revision because the players/net/rackets/shuttlecock and
spectator/bench roles were not readable enough. Those runs have zero Seedance
calls and remain Proxy-stage failures.
