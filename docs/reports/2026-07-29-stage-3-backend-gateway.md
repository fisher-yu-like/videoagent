# Stage 3 实验报告：Backend Gateway

日期：2026-07-29  
状态：部分通过（Kling prompt-only 基线通过；proxy 注入仍被真实门控阻塞）

## 结论

基于仓库已有 `kling_demo.py` 和 `seedance_demo.py`，项目已形成一层薄的后端能力矩阵、payload builder、提交就绪检查和无自动重试 JD 客户端。唯一一次 Kling prompt-only POST 实际提交成功，经过四次 GET 查询达到 `success`，下载得到可播放 MP4 并完成 Blender 元数据与首/中/尾帧目视检查。

Seedance 没有再次提交。首尾帧缺少真实远程资产绑定，JD 网关的 `reference_video` 字段仍没有可核验的官方网关证据，因此 proxy 注入没有被错误放行。

## Baseline 与参考源码继承

- `kling_demo.py`：继承 `Kling-V2-5-Turbo`、`/v1/task/submit`、`/v1/task/{task_id}`、text/image content、异步状态和结果 URL 形状。
- `seedance_demo.py`：继承 `Doubao-Seedance-2.0`、首帧/尾帧 role、reference image/audio 和网关调用形状。
- 新代码只提取公共解析、记录和门控功能，没有引入多智能体运行时或重写后端框架。
- Camera Artist/SceneCraft 继续只影响结构化导演规划与 Blender 可执行代理；VACE 仍保留为后续 RGB/depth/pose/mask 开源控制 baseline。

火山引擎官方材料说明 Seedance 2.0 模型层面支持文字、图片、音频、视频四种模态输入，但这不能证明 JD 网关透传同一字段：[Seedance 2.0 官方发布说明](https://developer.volcengine.com/articles/7628567056649125942)。官方素材说明还展示了经审核素材的 `asset://<asset_id>` 用法：[火山方舟可信素材说明](https://www.volcengine.com/docs/82379/2315856)。

## 提交前 readiness

输入：`runs/stage2_control_bridge/control_bundle.json`。  
输出：`runs/stage3_backend/readiness.json`。

| 条件 | 状态 | 原因 |
|---|---|---|
| plain prompt | ready | 可按现有 Seedance client 构造 |
| cinematic prompt | ready | 可按现有 Seedance client 构造 |
| first/last frame | blocked | 缺少 first/last 的真实远程绑定 |
| proxy video | blocked | 缺少远程绑定；JD `reference_video` 未验证 |

该命令明确记录 `network_called=false`，没有 task ID 或虚构 URL。

## 唯一一次真实 Kling 提交

请求条件：

- 后端：JD Cloud Gateway；
- 模型：`Kling-V2-5-Turbo`；
- shot：s01；
- prompt：Stage 2 cinematic prompt；
- 参数：5 秒、`std`、16:9；
- 图片、视频、音频输入：无；
- submit 自动重试：0。

真实结果：

- POST 次数：1；
- task ID：`task-ebzlp2kd3xv3tqy`；
- 提交响应：`任务提交成功`；
- GET 次数：4；
- 状态序列：`pending → running → running → success`；
- 最终 error code：0；
- 最终 `usage.video_output`：1.5，网关未提供其计费单位，因此不解释为金额；
- CDN 下载次数：1。

## 下载产物

- 路径：`runs/stage3_api/20260729T012120Z_kling_d8bde2ef/result.mp4`
- 字节数：7,207,519
- SHA-256：`f3a95d76e898858726be8651f0472bafd0ff6a33e6c0e6743f5e23730aae35f3`
- Blender 读取：1280×720、121 帧、24 fps、5.042 秒。

检查帧：

| 帧 | SHA-256 |
|---|---|
| first | `6af45a73f417732b36e85fecc4d45bf8aff74bdb67b7c0d654ea82e85b5ad75c` |
| middle | `5923ab1b9910cb0a68c1e9d89e4e9fd90345ca42d0cb91bbce44dc1f1972300f` |
| last | `378179169ecbbf4259c1c936e0beb6050197e6a24aac1de7e79a07e5ce759719` |

## 真实画面观察

- 场景正确生成了火车站站台、两名人物和宽景构图。
- 男性从画面左侧、女性从右侧相向靠近；中帧会面，末帧出现握手，满足“two people meeting”的主要叙事。
- A 的主要屏幕运动为左向右，没有观察到明显轴线翻转。
- 首/中/尾帧中中央长椅和站牌基本保持居中，prompt 指定的 `camera truck right` 不明显，不能判定相机运动控制通过。
- 站牌文字是不可读的生成乱码。

因此本次真实实验只证明 prompt-only 语义基线可用；它同时暴露出纯文字运镜控制不足，不能作为 camera-control 或 proxy-injection 成功证据。

## 安全与资源

- Stage 3 只有一次生成 POST，没有自动提交重试。
- Seedance 上一次结果未知，本阶段未重试。
- 运行目录扫描未发现 `JD_KLING_KEY`。
- 未使用服务器、A100 或其他租赁 GPU。
- 没有上传 Stage 2 的 Blender proxy、人物参考或真实拍摄素材。

## 下一门槛

进入首尾帧真实实验前，需要获得真实可访问的 HTTPS URL 或网关认可的 `asset://` ID。进入 proxy-video 实验前，还必须从 JD 文档或一次受控网关验证中确认 `reference_video` 的准确 content 字段。两项条件未满足时，提交代码保持 blocked。
