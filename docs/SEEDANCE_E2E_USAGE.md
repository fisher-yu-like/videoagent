# Seedance 多视角全链路使用说明

1. 准备三条同一 Blender 世界渲染出的 Proxy，脚本会自动规范化为 854×480、24 fps、H.264 无音频。
2. 生产环境设置 TOS：`TOS_ACCESS_KEY`、`TOS_SECRET_KEY`、`TOS_BUCKET`、`TOS_ENDPOINT`、`TOS_REGION`。
3. 研究临时运行可显式设置 `$env:VIDEOACTAGENT_TEMP_UPLOAD="1"`；它使用第三方临时托管，链接会过期，不适合长期保存。
4. 运行：

```powershell
python scripts/run_seedance_e2e.py --root runs/results/<run> --appearance-prompt runs/results/<proxy-run>/appearance_prompt_bundle.json --model Doubao-Seedance-2.0 --limit 4 --refresh-temp-uploads
```

每个任务固定提交一次。`request.json`、查询响应、`task_id`、下载 MP4、SHA-256 和 ffprobe 信息会保存在任务目录；生成完后运行：

```powershell
python scripts/collect_seedance_multiview_results.py --output runs/results/seedance_multiview_e2e_final_20260809
```

最终目录的 `proxy/`、`prompts/` 和 `videos/` 可直接查看，`manifest.json` 是唯一结果索引。`Doubao-Seedance-2.5` 若被网关拒绝，不要改 seed 重试，应使用已授权模型。
