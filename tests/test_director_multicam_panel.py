from pathlib import Path
import subprocess
import unittest


class DirectorMulticamPanelTests(unittest.TestCase):
    def setUp(self):
        self.html = Path("static/director_multicam_panel.html").read_text(
            encoding="utf-8"
        )

    def test_independent_multicam_page_contract(self):
        html = self.html
        for element_id in (
            "stage-staging", "stage-camera", "stage-result", "target-select",
            "add-object", "save-staging", "approve-staging",
            "undo-staging", "clear-staging-drawing", "staging-dirty",
            "proxy-camera-a", "proxy-camera-b", "proxy-camera-c", "sync-play",
            "coverage-timeline", "camera-tabs", "trajectory-plane", "agent-plan",
            "approve-plan", "render-proxy", "approve-proxy", "status",
        ):
            self.assertIn(f'id="{element_id}"', html)
        self.assertIn("/api/plans", html)
        self.assertIn("/api/staging", html)
        self.assertIn("/approve", html)
        self.assertIn('class="hint"', html)
        self.assertIn("scrollIntoView", html)
        self.assertIn("三台摄像机同步播放", html)
        for javascript_contract in (
            "savedTrajectory", "editHistory", "pushHistory",
            "undoStaging", "clearSelectedDrawing", "advanceStagingKeyframe",
        ):
            self.assertIn(javascript_contract, html)
        self.assertIn("本次编辑：无未保存修改", html)
        self.assertIn("本次编辑：有未保存修改", html)
        self.assertIn("当前没有未保存修改", html)
        self.assertNotEqual(
            html, Path("static/director_panel.html").read_text(encoding="utf-8")
        )

    def test_page_contains_prompt_scene_plan_and_reference_stages(self):
        for element_id in (
            "wizard-progress",
            "stage-prompt", "story-prompt", "story-duration", "generate-scene",
            "stage-scene-plan", "scene-template", "actor-editor", "object-editor",
            "initial-camera-editor", "save-scene-plan", "approve-scene-plan",
            "scene-feedback", "rewrite-scene-plan",
            "stage-reference-review", "reference-review-video",
            "return-to-scene-plan", "reference-feedback", "rewrite-reference",
            "render-reference", "approve-reference",
        ):
            self.assertIn(f'id="{element_id}"', self.html)

    def test_scene_plan_uses_repeatable_rows_and_simple_camera_fields(self):
        for element_id in (
            "add-scene-actor", "add-scene-object", "camera-shot-size",
            "camera-focal-length", "camera-motion", "camera-look-at",
            "world-min-x", "world-max-x", "world-min-y", "world-max-y",
            "camera-start-x", "camera-start-y", "camera-start-z",
            "camera-end-x", "camera-end-y", "camera-end-z",
        ):
            self.assertIn(f'id="{element_id}"', self.html)
        self.assertIn("addActorRow", self.html)
        self.assertIn("addObjectRow", self.html)
        self.assertIn("readScenePlanForm", self.html)

    def test_scene_template_options_match_the_scene_plan_contract(self):
        for preset in (
            "station", "city_crosswalk", "forest_path", "studio_room",
            "cafe", "warehouse", "generic",
        ):
            self.assertIn(f'<option value="{preset}">', self.html)

    def test_camera_motion_options_match_the_closed_scene_plan_contract(self):
        for motion in (
            "static", "follow", "arc", "dolly", "truck", "dolly_in",
            "dolly_out", "truck_left", "truck_right", "pan_left", "pan_right",
        ):
            self.assertIn(f'<option value="{motion}">', self.html)
        self.assertNotIn('<option value="pan">', self.html)
        self.assertNotIn('<option value="track">', self.html)

    def test_scene_plan_dirty_state_blocks_approval_until_save_refreshes(self):
        self.assertIn('id="scene-plan-dirty"', self.html)
        for javascript_contract in (
            "sceneFormDirty", "updateSceneEditState", "场景参数有未保存修改",
            "dirty?'场景参数：有未保存修改'",
            "session.approved_scene_plan===planId||dirty",
        ):
            self.assertIn(javascript_contract, self.html)

    def test_initial_color_input_normalization_does_not_dirty_saved_scene_plan(self):
        for javascript_contract in (
            "canonicalScenePlan",
            "actor.color=actor.color.toUpperCase()",
            "canonicalScenePlan(readScenePlanForm())",
            "canonicalScenePlan(saved)",
        ):
            self.assertIn(javascript_contract, self.html)
        self.assertIn(
            "session.approved_scene_plan===planId||dirty",
            self.html,
        )

    def test_scene_canonicalization_ignores_nested_object_key_order(self):
        helper = self.html.split("function canonicalScenePlan", 1)[1].split(
            "function sceneFormDirty", 1
        )[0]
        javascript = """
function clone(value){return JSON.parse(JSON.stringify(value))}
function canonicalScenePlan%s
const saved={
  schema_version:'scene-plan-1.0',
  actors:[{id:'traveler',color:'#F28E2B',start:[-3,0,0]}],
  initial_camera:{shot_size:'wide',start:[0,-10,6]}
};
const form={
  initial_camera:{start:[0,-10,6],shot_size:'wide'},
  actors:[{start:[-3,0,0],color:'#f28e2b',id:'traveler'}],
  schema_version:'scene-plan-1.0'
};
process.stdout.write(String(
  JSON.stringify(canonicalScenePlan(saved))===
  JSON.stringify(canonicalScenePlan(form))
));
""" % helper
        result = subprocess.run(
            ["node", "-e", javascript],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.stdout, "true")

    def test_progress_function_gates_all_seven_workflow_states(self):
        for javascript_contract in (
            "wizardStepNumber", "session.approved_plan", "session.current_iteration",
            "session.workflow_step==='complete'", "setStageAccess", "aria-disabled",
            "相机规划", "多机位审看", "完成",
        ):
            self.assertIn(javascript_contract, self.html)

    def test_api_count_and_camera_json_are_in_collapsible_technical_details(self):
        self.assertIn('id="api-call-count"', self.html)
        self.assertIn("session.api_call_count", self.html)
        self.assertIn('id="camera-technical-details"', self.html)
        self.assertIn("技术详情（Camera Agent JSON）", self.html)
        self.assertIn("addSourceHash", self.html)
        self.assertNotIn("source_hash:scenePlanRecord()?.source_hash||", self.html)
        self.assertNotIn("source_hash:reference.source_hash||", self.html)

    def test_wizard_calls_versioned_approval_apis_and_gates_trajectory_setup(self):
        for javascript_contract in (
            "/api/scene-plans",
            "/api/references/",
            "/api/jobs/reference-",
            "session.workflow_step",
            "initializeTrajectoryStages",
            "session.approved_reference",
        ):
            self.assertIn(javascript_contract, self.html)

    def test_wizard_shows_seven_steps_and_collapsible_audit_logs(self):
        self.assertEqual(self.html.count('class="wizard-step"'), 7)
        self.assertIn('id="scene-audit-log"', self.html)
        self.assertIn('id="reference-audit-log"', self.html)
        self.assertIn("JSON / 来源哈希日志", self.html)

    def test_camera_editor_exposes_position_and_look_at_controls(self):
        for element_id in (
            "camera-edit-position", "camera-edit-look-at", "look-height",
            "revision-scope", "camera-feedback", "revise-camera-plan",
            "rerender-proxy",
        ):
            self.assertIn(f'id="{element_id}"', self.html)
        for scope in ("all", "camera_a", "camera_b", "camera_c"):
            self.assertIn(f'<option value="{scope}">', self.html)
        for javascript_contract in (
            "cameraEditMode", "editCameraPoint", "look_at[2]",
            "#00d8ff", "#58d68d", "setLineDash([8,6])",
        ):
            self.assertIn(javascript_contract, self.html)

    def test_result_feedback_calls_versioned_revision_and_rerender_apis(self):
        for javascript_contract in (
            "`/api/plans/${session.approved_plan}/revise`",
            "{scope:$('revision-scope').value,feedback}",
            "`/api/iterations/${session.current_iteration}/rerender`",
            "postJSON(`/api/iterations/${session.current_iteration}/rerender`,{})",
            "poll(j.job_id)",
            "$('revise-camera-plan').disabled=!session.approved_plan",
            "$('rerender-proxy').disabled=!session.current_iteration",
        ):
            self.assertIn(javascript_contract, self.html)

    def test_camera_point_editor_keeps_position_and_look_at_independent(self):
        self.assertIn("function editCameraPoint", self.html)
        helper = self.html.split("function editCameraPoint", 1)[1].split(
            "function drawCamera", 1
        )[0]
        javascript = """
function editCameraPoint%s
const state={position:[1,2,3],look_at:[4,5,6]};
const originalLook=JSON.stringify(state.look_at);
editCameraPoint(state,'position',[10,20]);
if(JSON.stringify(state.position)!==JSON.stringify([10,20,3]))process.exit(1);
if(JSON.stringify(state.look_at)!==originalLook)process.exit(2);
const originalPosition=JSON.stringify(state.position);
editCameraPoint(state,'look_at',[30,40]);
if(JSON.stringify(state.look_at)!==JSON.stringify([30,40,6]))process.exit(3);
if(JSON.stringify(state.position)!==originalPosition)process.exit(4);
process.stdout.write('independent');
""" % helper
        result = subprocess.run(
            ["node", "-e", javascript],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.stdout, "independent")


if __name__ == "__main__":
    unittest.main()
