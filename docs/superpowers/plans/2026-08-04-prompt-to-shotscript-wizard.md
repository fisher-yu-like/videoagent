# Prompt to ShotScript Wizard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不重写现有人工轨迹与多机位流程的前提下，增加一个从自然语言 Prompt 开始、经人工批准 ShotScript 和第一版 Proxy 后进入原流程的单页向导。

**Architecture:** 新增轻量 `director_wizard` 外层工作区，负责 Prompt、场景草案和 Reference 版本；每个批准的 Reference 都通过现有 `director_multicam.prepare_workspace()` 创建一个完整子工作区，后续轨迹、摄像机规划和三视角渲染全部调用现有函数。DeepSeek 场景规划输出先进入独立严格合同，再转换为现有 `ShotScript` 和 `TrajectoryInstruction`；Blender 仍执行仓库内固定编译器。

**Tech Stack:** Python 3.10+ 标准库、现有 dataclass/JSON 合同、DeepSeek OpenAI-compatible API、本地 Blender 5.1、原生 HTML/CSS/JavaScript、`unittest`。

---

## 文件边界

- 新建 `videoactagent/scene_plan.py`：LLM 场景草案的严格合同，以及到现有 ShotScript/轨迹合同的确定性转换。
- 修改 `videoactagent/deepseek_planner.py`：复用现有 URL、脱敏证据和零重试约束，新增场景草案请求；两类规划都固定使用 `deepseek-v4-flash`。
- 新建 `videoactagent/director_wizard.py`：只管理向导前置阶段和对子工作区的委托，不复制轨迹、相机或渲染业务。
- 修改 `videoactagent/shotscript.py` 与 `videoactagent/blender_proxy.py`：仅补充 `generic` 环境模板。
- 修改 `static/director_multicam_panel.html`：在原三个步骤前增加 Prompt、场景表单和 Reference 审批卡；原轨迹/相机/结果控件及事件保持原位。
- 修改 `videoactagent/cli.py`：增加一个 `director-wizard` 入口，保留两个旧入口。
- 修改 `README.md`：把一条启动命令和七步使用说明写清楚，并保留用户当前未提交的措辞修改。
- 新建对应测试文件；原测试只在合同确实变化时做最小更新。

### Task 1: 定义受约束 ScenePlanDraft 并复用现有合同

**Files:**
- Create: `videoactagent/scene_plan.py`
- Create: `tests/test_scene_plan.py`
- Modify: `videoactagent/shotscript.py`
- Modify: `videoactagent/blender_proxy.py`

- [ ] **Step 1: 写失败测试，明确草案到 ShotScript/轨迹的边界**

```python
from videoactagent.scene_plan import ScenePlanDraft, ScenePlanError
from videoactagent.shotscript import ShotScript
from videoactagent.trajectory import TrajectoryInstruction

VALID_DRAFT = {
    "schema_version": "scene-plan-1.0",
    "scene_id": "new_station_story",
    "environment_preset": "station",
    "duration_seconds": 5.0,
    "fps": 3,
    "world_bounds": [-5.0, 5.0, -4.0, 4.0],
    "actors": [
        {"id": "traveler", "color": "#F28E2B", "action": "walk",
         "start": [-3.0, 0.0, 0.0], "end": [-0.5, 0.0, 0.0], "facing": "friend"},
        {"id": "friend", "color": "#4E79A7", "action": "wait",
         "start": [1.5, 0.0, 0.0], "end": [1.5, 0.0, 0.0], "facing": "traveler"},
    ],
    "objects": [
        {"id": "suitcase", "primitive": "cube", "semantic": "move",
         "start": [-2.8, 0.2], "end": [-0.3, 0.2]},
    ],
    "initial_camera": {"shot_size": "wide", "focal_length_mm": 35,
        "motion": "static", "start": [0.0, -10.0, 6.0],
        "end": [0.0, -10.0, 6.0], "look_at": "actors_midpoint"},
    "explanation": "先用固定全景检查两人的会合调度。",
}

class ScenePlanTests(unittest.TestCase):
    def test_valid_scene_plan_compiles_to_existing_contracts(self):
        draft = ScenePlanDraft.from_dict(VALID_DRAFT)
        script = ShotScript.from_dict(draft.to_shotscript("原始故事"))
        trajectory = TrajectoryInstruction.from_dict(draft.to_trajectory())
        self.assertEqual(script.scene_id, "new_station_story")
        self.assertEqual(len(script.shots), 1)
        self.assertEqual(
            [track.target_id for track in trajectory.tracks],
            ["traveler", "friend", "suitcase"],
        )

    def test_scene_plan_rejects_out_of_bounds_actor(self):
        value = copy.deepcopy(VALID_DRAFT)
        value["actors"][0]["start"] = [50.0, 0.0, 0.0]
        with self.assertRaisesRegex(ScenePlanError, r"actors\[0\]\.start"):
            ScenePlanDraft.from_dict(value)
```

- [ ] **Step 2: 运行测试并确认因模块不存在而失败**

Run: `python -m unittest tests.test_scene_plan -v`  
Expected: `ModuleNotFoundError: No module named 'videoactagent.scene_plan'`

- [ ] **Step 3: 实现最小严格合同和确定性转换**

`scene_plan.py` 使用冻结 dataclass 定义 `SceneActor`、`SceneObject`、`InitialCamera`、`ScenePlanDraft`。`from_dict()` 要求字段精确、人物 1 至 3 个、ID 全局唯一、有限数值、十六进制颜色、起终点位于 bounds；preset 只接受现有六个精确 ID和 `generic`。`to_shotscript(prompt)` 输出一个 `whole` shot；`to_trajectory()` 复用 K0/K1/K2/K3/K4 时间 `(0,.2,.5,.8,1)` 并线性插值 actor/object。

核心公开接口固定为：

```python
class ScenePlanError(ValueError): ...

@dataclass(frozen=True)
class ScenePlanDraft:
    @classmethod
    def from_dict(cls, value: object) -> "ScenePlanDraft": ...
    def to_dict(self) -> dict[str, object]: ...
    def to_shotscript(self, story_prompt: str) -> dict[str, object]: ...
    def to_trajectory(self) -> dict[str, object]: ...
```

为 `generic` 只增加一个确定性环境函数：地面、后墙、四个边界柱和中性灯光；将它加入现有 `ENVIRONMENT_BUILDERS` 与 ShotScript allow-list，不改动其他模板。

- [ ] **Step 4: 运行新测试和原 ShotScript/Blender profile 测试**

Run: `python -m unittest tests.test_scene_plan tests.test_station_shotscript tests.test_blender_render_profiles -v`  
Expected: all tests pass.

- [ ] **Step 5: 提交合同层**

```powershell
git add videoactagent/scene_plan.py videoactagent/shotscript.py videoactagent/blender_proxy.py tests/test_scene_plan.py
git commit -m "feat: add constrained scene plan contract"
```

### Task 2: 在原 DeepSeek 适配器中增加 ScenePlan 请求

**Files:**
- Modify: `videoactagent/deepseek_planner.py`
- Modify: `tests/test_deepseek_planner.py`

- [ ] **Step 1: 写失败测试，固定模型、一次调用和真实证据字段**

```python
def test_scene_plan_uses_flash_once_and_validates_before_success(self):
    calls = []
    response = json.dumps({
        "id": "scene-call-1", "model": "deepseek-v4-flash",
        "choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps(VALID_DRAFT, ensure_ascii=False)}}],
        "usage": {"prompt_tokens": 40, "completion_tokens": 120},
    }).encode()
    def transport(request, timeout):
        calls.append(request)
        return FakeResponse(response)
    with tempfile.TemporaryDirectory() as root:
        result = request_scene_plan(
            story_prompt="一个旅客走向朋友",
            duration_seconds=5,
            output_dir=Path(root) / "SP1",
            environ={"DEEPSEEK_API_KEY": "secret",
                     "DEEPSEEK_BASE_URL": "https://example.test/v1"},
            transport=transport,
        )
        request_body = json.loads(calls[0].data)
    self.assertEqual(len(calls), 1)
    self.assertEqual(request_body["model"], "deepseek-v4-flash")
    self.assertEqual(result["api_call_count"], 1)
    self.assertEqual(result["retry_count"], 0)
```

同时给原 `request_multicam_plan()` 增加断言 `request_payload["model"] == "deepseek-v4-flash"`，保证后续 Camera A/B/C 也使用同一模型。

- [ ] **Step 2: 运行测试并确认缺少 `request_scene_plan`**

Run: `python -m unittest tests.test_deepseek_planner -v`  
Expected: import or attribute failure for `request_scene_plan`.

- [ ] **Step 3: 最小复用现有请求基础设施**

在同一文件增加 `MODEL = "deepseek-v4-flash"`、`SCENE_JSON_PROMPT` 和 `request_scene_plan()`。复用 `_endpoint()`、`_failure_evidence()`、`_write()`、现有 `Request/urlopen`、finish_reason 检查和 evidence 结构；不得从 `DEEPSEEK_MODEL` 改写模型，不增加重试或 fallback。解析 content 后必须执行 `ScenePlanDraft.from_dict()`，只有成功才写 `draft.json` 和 succeeded evidence。

公开签名：

```python
def request_scene_plan(
    *, story_prompt: str, duration_seconds: float, output_dir: Path | str,
    feedback: str | None = None, previous_draft: Mapping[str, Any] | None = None,
    environ: Mapping[str, str] | None = None,
    transport: Callable[..., Any] = urlopen,
) -> dict[str, Any]: ...
```

- [ ] **Step 4: 验证两类 DeepSeek 调用测试**

Run: `python -m unittest tests.test_deepseek_planner tests.test_multicam_plan -v`  
Expected: all tests pass; every fake transport is called at most once.

- [ ] **Step 5: 提交适配器**

```powershell
git add videoactagent/deepseek_planner.py tests/test_deepseek_planner.py
git commit -m "feat: generate scene plans with deepseek flash"
```

### Task 3: 用外层向导委托给现有 multicam 工作区

**Files:**
- Create: `videoactagent/director_wizard.py`
- Create: `tests/test_director_wizard.py`

- [ ] **Step 1: 写失败测试，证明版本门禁和现有函数被复用**

```python
def test_approved_scene_plan_builds_existing_multicam_workspace():
    with tempfile.TemporaryDirectory() as root:
        wizard = create_workspace(Path(root) / "blender.exe", Path(root) / "project")
        plan = save_scene_plan(wizard, VALID_DRAFT, prompt="新故事", parent=None)
        approve_scene_plan(wizard, plan["scene_plan_id"], author_id="human")
        with patch("videoactagent.director_wizard.prepare_workspace") as prepare:
            prepare.return_value = Path(root) / "project/references/R1/pipeline/multicam_manifest.json"
            reference = render_reference(wizard, plan["scene_plan_id"])
        prepare.assert_called_once()
        self.assertEqual(reference["reference_id"], "R1")

def test_staging_is_hidden_until_reference_is_approved():
    session = session_document(wizard)
    self.assertEqual(session["workflow_step"], "reference_review")
    self.assertNotIn("staging", session)
```

另写测试覆盖：本地表单保存不调用 DeepSeek；Agent feedback 产生新 SP；未批准 SP 不能渲染；批准旧版本后新后续版本标记 stale；来源哈希被修改后拒绝；旧 `director_multicam` manifest 仍由原入口读取。

- [ ] **Step 2: 运行测试并确认模块不存在**

Run: `python -m unittest tests.test_director_wizard -v`  
Expected: `ModuleNotFoundError`.

- [ ] **Step 3: 实现外层工作区，不复制后半程业务**

实现以下函数：

```python
def create_workspace(blender_path: Path | str, output_dir: Path | str) -> Path: ...
def generate_scene_plan(manifest_path, prompt, duration_seconds, *, feedback=None) -> dict: ...
def save_scene_plan(manifest_path, draft, *, prompt, parent) -> dict: ...
def approve_scene_plan(manifest_path, scene_plan_id, *, author_id) -> Path: ...
def render_reference(manifest_path, scene_plan_id) -> dict: ...
def approve_reference(manifest_path, reference_id, *, author_id) -> Path: ...
def session_document(manifest_path) -> dict[str, object]: ...
```

`render_reference()` 将批准 SP 的 `shotscript.json` 和 `trajectory.json` 传给现有 `prepare_workspace()`；只用一个自定义 `reference_renderer` 给现有 `blender_runner` 增加 `--trajectory` 参数，以便关键物体也显示。随后调用现有 `save_staging()` 生成包含物体轨迹的当前 S 版本。Reference 批准后，`session_document()` 直接合并现有 `director_multicam.session_document()`，后续保存/批准轨迹、生成/批准相机方案、渲染/批准 M 版本全部调用原函数。

- [ ] **Step 4: 验证外层版本与旧流程兼容**

Run: `python -m unittest tests.test_director_wizard tests.test_director_multicam tests.test_director_multicam_compat -v`  
Expected: all tests pass.

- [ ] **Step 5: 提交向导控制器**

```powershell
git add videoactagent/director_wizard.py tests/test_director_wizard.py
git commit -m "feat: add versioned director wizard workflow"
```

### Task 4: 给同一页面增加前三个审批步骤

**Files:**
- Modify: `static/director_multicam_panel.html`
- Modify: `tests/test_director_multicam_panel.py`
- Create: `tests/test_director_wizard_http.py`

- [ ] **Step 1: 写失败的页面与 HTTP 合同测试**

```python
class DirectorWizardPanelTests(unittest.TestCase):
    def test_page_contains_scene_and_reference_stages(self):
        html = Path("static/director_multicam_panel.html").read_text(encoding="utf-8")
        for element_id in (
            "stage-prompt", "story-prompt", "story-duration", "generate-scene",
            "stage-scene-plan", "scene-template", "actor-editor",
            "save-scene-plan", "approve-scene-plan", "stage-reference-review",
            "reference-review-video", "approve-reference", "scene-feedback",
        ):
            self.assertIn(f'id="{element_id}"', html)
```

HTTP 集成测试用临时目录和 fake planner/renderer 验证：`POST /api/scene-plans`、本地保存、SP 批准、Reference 渲染、Reference 批准，以及批准后原 `/api/staging` 路由仍可用。

- [ ] **Step 2: 运行并确认新控件和路由缺失**

Run: `python -m unittest tests.test_director_multicam_panel tests.test_director_wizard_http -v`  
Expected: failures naming missing stage-prompt and `/api/scene-plans`.

- [ ] **Step 3: 扩展原页面并实现最少路由**

保留现有 CSS、canvas、视频同步和原事件处理。在顶部新增进度条及三张 card；actor/object 表单用可重复行，不暴露 JSON。`refresh()` 根据 `workflow_step` 展开当前 card；只有 Reference 批准后才初始化原 `trajectory` 变量和原三个阶段。

`director_wizard.py` 的 Handler 新增：

```text
GET  /api/session
POST /api/scene-plans
POST /api/scene-plans/{SP}/save
POST /api/scene-plans/{SP}/approve
POST /api/scene-plans/{SP}/render
POST /api/references/{R}/approve
```

后续路径通过调用/委托原 `_Handler` 处理；错误统一返回真实 JSON `{error: ...}`。Reference 渲染在线程中执行并通过 `/api/jobs/reference-{R}` 轮询，避免浏览器请求阻塞 Blender。

- [ ] **Step 4: 验证页面、HTTP 与原 UI 合同**

Run: `python -m unittest tests.test_director_multicam_panel tests.test_director_wizard_http tests.test_director_multicam_compat -v`  
Expected: all tests pass; original `director-loop` HTML remains byte-unchanged.

- [ ] **Step 5: 提交前端**

```powershell
git add static/director_multicam_panel.html videoactagent/director_wizard.py tests/test_director_multicam_panel.py tests/test_director_wizard_http.py
git commit -m "feat: add prompt first director wizard UI"
```

### Task 5: 增加单命令启动并更新简明中文说明

**Files:**
- Modify: `videoactagent/cli.py`
- Modify: `tests/test_cli.py`
- Modify: `README.md`

- [ ] **Step 1: 写失败测试，保留所有旧入口**

```python
def test_help_lists_new_wizard_without_removing_old_directors(self):
    self.assertEqual(COMMANDS["director-wizard"], "videoactagent.director_wizard")
    self.assertIn("director-loop", COMMANDS)
    self.assertIn("director-multicam", COMMANDS)
```

- [ ] **Step 2: 运行并确认新入口缺失**

Run: `python -m unittest tests.test_cli -v`  
Expected: failure for missing `director-wizard`.

- [ ] **Step 3: 实现一条启动命令并合并 README 用户改动**

新增命令：

```powershell
python -m videoactagent.cli director-wizard start --blender D:\blender\blender.exe --workspace runs\work\my_story --port 8770
```

`start` 在 workspace 不存在时创建，存在时恢复；禁止覆盖不匹配的 Blender 路径。README 只说明环境变量、这一条命令、七步按钮流程、结果目录和失败含义；保留当前工作树中“完整 5 秒 Blender 参考 Proxy及人工修改”的用户修改。

- [ ] **Step 4: 验证 CLI 和文档差异**

Run: `python -m unittest tests.test_cli tests.test_director_multicam_compat -v`  
Expected: all tests pass.

Run: `git diff --check`  
Expected: no whitespace errors.

- [ ] **Step 5: 提交入口和文档**

```powershell
git add videoactagent/cli.py tests/test_cli.py README.md
git commit -m "docs: add one command director wizard guide"
```

### Task 6: 全量自动化回归和真实本地验收

**Files:**
- Create: `runs/work/prompt_wizard_acceptance_20260804/`（真实运行证据，不作为单元测试素材）
- Modify: `README.md`（仅在真实命令与实际界面不一致时修正）

- [ ] **Step 1: 运行完整测试集**

Run: `python -m unittest discover -s tests -v`  
Expected: zero failures and zero errors.

- [ ] **Step 2: 用真实 DeepSeek 只生成一个全新场景草案**

从前端输入一个未用于测试夹具的新故事，点击一次“生成场景草案”。检查 `evidence.json` 的 `model == deepseek-v4-flash`、`api_call_count == 1`、`retry_count == 0`，并人工确认表单内容后批准。若 API 失败，保留失败证据并报告，不把 fake response 当实验结果。

- [ ] **Step 3: 用本机 Blender 渲染真实第一版 Reference**

批准 ShotScript 后从前端触发一次渲染。使用 `ffprobe` 核对 MP4 非零、时长等于 ShotScript、帧数等于 `duration × fps`；核对 `.blend`、日志、轨迹报告和来源哈希。人工观看后批准或明确记录不满意原因。

- [ ] **Step 4: 完成原有后半程真实链路**

在原 canvas 修改或沿用人物/物体轨迹并人工批准；点击一次三机位规划；批准后渲染 Camera A/B/C 三条完整 Blender 视频。核对三条视频时长、FPS、帧数一致，保存真实自动检查和人工结论。

- [ ] **Step 5: 做完成前证据审计**

Run: `git status --short`  
Expected: only intentional files or真实运行输出；不得误提交 API 密钥和临时日志。

Run: `python -m unittest discover -s tests -v`  
Expected: zero failures and zero errors.

Run: `git diff --check HEAD~5..HEAD`  
Expected: no whitespace errors.

- [ ] **Step 6: 提交必要的验收后修正**

```powershell
git add README.md videoactagent static tests
git commit -m "test: verify prompt first director workflow"
```

只在确有修正时执行该提交；真实视频与包含 API 原始响应的运行目录默认不提交 Git。
