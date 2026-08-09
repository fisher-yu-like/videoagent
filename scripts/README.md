# 实验入口

主线入口：

- `run_complex_scene_suite.py`：Prompt → shared-world Blender Proxy → ProxyVerifier，可选择 canonical/skeleton 结构预演。
- `run_complex_pipeline_v2.py`：Pipeline v2 的通用本地链路。
- `run_seedance_e2e.py`：Proxy 通过后，按 camera 独立提交 Seedance reference-video。
- `run_complex_multiview_e2e.py`：多机位结果收集和证据绑定。

其余脚本是历史实验或特定后端探针，不属于默认入口；运行前应先阅读 `docs/CURRENT_PIPELINE_AND_RESULTS.md`，不要把 debug patch 脚本当成主 pipeline。
