# Fixed Character Asset Library Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add versioned rigged character assets to the existing VideoActAgent Proxy pipeline so Blender instantiates stable male/female characters and applies planned trajectories/cameras without regenerating human geometry.

**Architecture:** Introduce a small asset-catalog and rig-mapping layer. Scene entities reference `asset_id`; the new `asset_humanoid` Blender branch loads the catalog asset into one shared world, applies existing root/bone/IK tracks, and emits asset hashes/logs. Existing `clay`, `canonical`, `storyhuman`, and `skeleton` branches remain unchanged and runnable.

**Tech Stack:** Python 3.12, Blender Python/CLI, existing `pipeline_v2` contracts, JSON sidecars, pytest, SHA-256, ffprobe/blackdetect.

---

## File Map

- Create: `videoactagent/asset_catalog.py` — typed catalog loading, schema validation, immutable materialization, SHA-256.
- Create: `videoactagent/rig_mapping.py` — unified bone names and asset-specific bone mapping checks.
- Create: `assets/characters/catalog.json` — versioned asset entries and required files.
- Create: `assets/characters/README.md` — how to add licensed GLB/Blend assets without changing trajectory contracts.
- Create: `assets/characters/human_male_v1/profile.json` and `rig_map.json` — probe profile mapped to the existing CesiumMan source.
- Create: `assets/characters/human_female_v1/profile.json` and `rig_map.json` — schema entry whose real GLB must be supplied before a female run is claimed successful; missing files fail closed as `asset_missing`.
- Modify: `videoactagent/complex_scene_prompts_v2.py` — add `asset_id` to character entities while preserving existing tracks and camera plans.
- Modify: `scripts/run_complex_scene_suite.py` — add the isolated `asset_humanoid` render branch, catalog materialization, and run-level checks.
- Modify: `pipeline_v2/proxy_verifier.py` — register deterministic asset/shared-world checks without changing old verifier schemas.
- Modify: `tests/test_complex_scene_suite.py` — catalog, asset IDs, branch isolation, and four-camera contracts.
- Create: `tests/test_asset_catalog.py` and `tests/test_rig_mapping.py` — unit tests for fail-closed catalog and bone mapping behavior.
- Modify: `docs/CURRENT_PIPELINE_AND_RESULTS.md` and `README.md` — document the new branch, asset prerequisites, and fallback behavior.

Before the real dual-character run, place two licensed, compatible files at:

```text
assets/characters/human_male_v1/model.glb
assets/characters/human_female_v1/model.glb
```

The existing `assets/canonical_humanoid/CesiumMan.glb` is used only as the male interface probe. It must not be described as a realistic male/female pair.

### Task 1: Add the catalog contract

**Files:**
- Create: `videoactagent/asset_catalog.py`
- Create: `assets/characters/catalog.json`
- Create: `assets/characters/README.md`
- Create: `tests/test_asset_catalog.py`

- [ ] **Step 1: Write failing catalog tests**

```python
def test_catalog_loads_versioned_asset_and_reports_sha256(tmp_path):
    catalog = load_catalog(tmp_path / "catalog.json")
    asset = catalog.require("human_male_v1")
    assert asset.kind == "rigged_humanoid"
    assert len(asset.source_sha256) == 64

def test_catalog_fails_closed_for_missing_female_model(tmp_path):
    catalog = load_catalog(tmp_path / "catalog.json")
    with pytest.raises(AssetMissingError):
        catalog.materialize("human_female_v1", tmp_path / "run")
```

- [ ] **Step 2: Run the tests and verify failure**

Run: `.venv\Scripts\python.exe -m pytest -q tests/test_asset_catalog.py`

Expected: FAIL because `asset_catalog.py` and catalog schema do not exist.

- [ ] **Step 3: Implement the minimal catalog API**

Implement these stable interfaces:

```python
@dataclass(frozen=True)
class AssetSpec:
    asset_id: str
    kind: str
    model_path: Path
    profile_path: Path
    rig_map_path: Path
    license_path: Path
    source_sha256: str

class AssetCatalog:
    def require(self, asset_id: str) -> AssetSpec: ...
    def materialize(self, asset_id: str, run_assets_dir: Path) -> Path: ...
```

`load_catalog()` must resolve paths from the repository root, verify every required file, recompute the model SHA-256, and raise `AssetMissingError` or `AssetIntegrityError`; it must never silently substitute another character.

- [ ] **Step 4: Run the tests and verify pass**

Run: `.venv\Scripts\python.exe -m pytest -q tests/test_asset_catalog.py`

Expected: PASS for catalog loading, hash binding, and missing-asset failure.

- [ ] **Step 5: Commit**

```bash
git add videoactagent/asset_catalog.py assets/characters tests/test_asset_catalog.py
git commit -m "add versioned character asset catalog"
```

### Task 2: Add unified rig mapping

**Files:**
- Create: `videoactagent/rig_mapping.py`
- Create: `assets/characters/human_male_v1/profile.json`
- Create: `assets/characters/human_male_v1/rig_map.json`
- Create: `assets/characters/human_female_v1/profile.json`
- Create: `assets/characters/human_female_v1/rig_map.json`
- Create: `tests/test_rig_mapping.py`

- [ ] **Step 1: Write failing mapping tests**

```python
def test_unified_mapping_requires_root_hands_and_feet():
    mapping = load_rig_map(Path("assets/characters/human_male_v1/rig_map.json"))
    assert set(REQUIRED_UNIFIED_BONES) <= set(mapping.unified_to_asset)

def test_mapping_rejects_duplicate_asset_bones(tmp_path):
    with pytest.raises(RigMappingError):
        load_rig_map(tmp_path / "duplicate.json")
```

- [ ] **Step 2: Run and confirm failure**

Run: `.venv\Scripts\python.exe -m pytest -q tests/test_rig_mapping.py`

Expected: FAIL because the loader and mapping files are absent.

- [ ] **Step 3: Implement mapping validation**

Define `REQUIRED_UNIFIED_BONES = ("root", "pelvis", "spine", "head", "upper_arm.L", "forearm.L", "hand.L", "upper_arm.R", "forearm.R", "hand.R", "upper_leg.L", "lower_leg.L", "foot.L", "upper_leg.R", "lower_leg.R", "foot.R")`. `load_rig_map()` validates one-to-one mappings, finite scale/orientation metadata, and explicit asset version.

- [ ] **Step 4: Run and verify pass**

Run: `.venv\Scripts\python.exe -m pytest -q tests/test_rig_mapping.py`

Expected: PASS, including duplicate and missing-bone rejection.

- [ ] **Step 5: Commit**

```bash
git add videoactagent/rig_mapping.py assets/characters tests/test_rig_mapping.py
git commit -m "add unified humanoid rig mapping"
```

### Task 3: Bind scene entities to asset IDs

**Files:**
- Modify: `videoactagent/complex_scene_prompts_v2.py`
- Modify: `tests/test_complex_scene_suite.py`

- [ ] **Step 1: Add failing scene-contract tests**

```python
def test_market_characters_have_explicit_asset_ids():
    spec = scene_spec("indoor_market_exchange")
    characters = [e for e in spec["entities"] if e["kind"] == "character"]
    assert {e["asset_id"] for e in characters} == {"human_male_v1", "human_female_v1"}

def test_asset_ids_do_not_change_trajectory_points_or_cameras():
    before = scene_spec("indoor_market_exchange")
    after = scene_spec("indoor_market_exchange")
    assert before["tracks"] == after["tracks"]
    assert before["cameras"] == after["cameras"]
```

- [ ] **Step 2: Run and confirm failure**

Run: `.venv\Scripts\python.exe -m pytest -q tests/test_complex_scene_suite.py -k asset_id`

Expected: FAIL because character entities do not yet carry asset IDs.

- [ ] **Step 3: Add IDs without changing motion or camera data**

Assign `human_male_v1` to the customer and `human_female_v1` to vendor/helper. Keep every existing `tracks`, `gesture_tracks`, `action_phases`, and `cameras` value byte-for-byte equivalent apart from the new entity field.

- [ ] **Step 4: Run and verify pass**

Run: `.venv\Scripts\python.exe -m pytest -q tests/test_complex_scene_suite.py -k asset_id`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add videoactagent/complex_scene_prompts_v2.py tests/test_complex_scene_suite.py
git commit -m "bind scene characters to reusable asset IDs"
```

### Task 4: Add the isolated Blender asset_humanoid branch

**Files:**
- Modify: `scripts/run_complex_scene_suite.py`
- Modify: `tests/test_complex_scene_suite.py`

- [ ] **Step 1: Write failing branch-isolation tests**

```python
def test_blender_script_contains_asset_humanoid_branch_and_logs():
    script = _blender_script()
    assert '"asset_humanoid"' in script
    assert "asset_log.json" in script
    assert "rig_map.json" in script

def test_existing_proxy_choices_remain_available():
    assert {"clay", "canonical", "storyhuman", "skeleton", "asset_humanoid"} <= render_style_choices()
```

- [ ] **Step 2: Run and confirm failure**

Run: `.venv\Scripts\python.exe -m pytest -q tests/test_complex_scene_suite.py -k asset_humanoid`

Expected: FAIL because the new branch and choices do not exist.

- [ ] **Step 3: Implement prefab instantiation**

Add `render_style_choices() -> tuple[str, ...]` returning `("clay", "canonical", "storyhuman", "skeleton", "asset_humanoid", "diagnostic")`, use it for the CLI choices, and add `asset_humanoid` to the branch only. In the generated Blender script, load `asset_registry.json`, call `AssetCatalog.materialize()` before creating entities, import each GLB once into a role-named collection, create a stable root empty, and apply the existing root/bone/IK tracks through the unified mapping. Do not modify the old branch conditionals. Write one row per character to `asset_log.json` containing `entity_id`, `asset_id`, `asset_sha256`, `rig_map_sha256`, and imported object count.

- [ ] **Step 4: Run and verify pass**

Run: `.venv\Scripts\python.exe -m pytest -q tests/test_complex_scene_suite.py -k asset_humanoid`

Expected: PASS and all existing proxy-choice regression tests remain green.

- [ ] **Step 5: Commit**

```bash
git add scripts/run_complex_scene_suite.py tests/test_complex_scene_suite.py
git commit -m "add fixed asset humanoid Blender branch"
```

### Task 5: Add deterministic asset and shared-world verifiers

**Files:**
- Modify: `pipeline_v2/proxy_verifier.py`
- Modify: `scripts/run_complex_scene_suite.py`
- Create: `tests/test_asset_verifiers.py`

- [ ] **Step 1: Write failing verifier tests**

```python
def test_shared_world_identity_fails_when_camera_hashes_differ(tmp_path):
    report = verify_shared_world_identity(tmp_path / "asset_log.json", expected_camera_count=4)
    assert report["status"] == "failed"

def test_asset_catalog_materialization_requires_every_character():
    report = verify_asset_catalog_materialization(registry, asset_log, proxy_style="asset_humanoid")
    assert report["status"] == "passed"
```

- [ ] **Step 2: Run and confirm failure**

Run: `.venv\Scripts\python.exe -m pytest -q tests/test_asset_verifiers.py`

Expected: FAIL because the verifier functions do not exist.

- [ ] **Step 3: Implement fail-closed checks**

Implement `verify_asset_catalog_materialization()` and `verify_shared_world_identity()`. The checks must reject missing entities, duplicate asset IDs, mismatched SHA-256 values, missing rig mappings, and camera logs that reference a different asset hash. For old proxy styles return `skipped`, preserving current semantics.

- [ ] **Step 4: Integrate into `run_scene()` and verify pass**

Append the checks to the existing ProxyVerifier report, write them into `scene_summary.json`, and run:

```bash
.venv\Scripts\python.exe -m pytest -q tests/test_asset_verifiers.py tests/test_complex_scene_suite.py
```

Expected: PASS with old branches skipped and `asset_humanoid` checked.

- [ ] **Step 5: Commit**

```bash
git add pipeline_v2/proxy_verifier.py scripts/run_complex_scene_suite.py tests/test_asset_verifiers.py
git commit -m "verify fixed asset materialization and shared identity"
```

### Task 6: Acquire and register real male/female assets

**Files:**
- Add only licensed binary assets under `assets/characters/human_male_v1/` and `assets/characters/human_female_v1/`.
- Modify: `assets/characters/catalog.json`, profiles, rig maps, and license records.
- Test: `tests/test_asset_catalog.py`, `tests/test_rig_mapping.py`.

- [ ] **Step 1: Verify the two source files are real and licensed**

Run the repository catalog validator. It must print two asset IDs, two non-empty model paths, two 64-character SHA-256 values, and two license records. If either model is missing, stop this task with `asset_missing`; do not create a fake GLB or claim a dual-character result.

- [ ] **Step 2: Map both rigs to the unified bone set**

Run the rig validator and require all 16 unified bones for both assets. Record axis, unit scale, rest-pose orientation, and source version in each profile.

- [ ] **Step 3: Commit only metadata and permitted assets**

```bash
git add assets/characters
git commit -m "register licensed male and female humanoid assets"
```

### Task 7: Run one real four-camera Proxy regression

**Files:**
- Modify: `docs/CURRENT_PIPELINE_AND_RESULTS.md`, `README.md`
- Create: `docs/FIXED_ASSET_HUMANOID_RUN_20260810.md`
- Output: new immutable directory under `runs/results/asset_humanoid_market_20260810/`

- [ ] **Step 1: Run deterministic preflight**

Run the asset catalog and rig mapping validators. Expected: both male/female assets present, hashes match catalog, no missing bone mappings.

- [ ] **Step 2: Render the shared world**

Run `scripts/run_complex_scene_suite.py` for `indoor_market_exchange` with `--proxy-style asset_humanoid`, existing Blender executable, the existing BVH/motion inputs, and four cameras. Do not call Seedance or Kling at this stage.

- [ ] **Step 3: Verify real media and logs**

Require four MP4 files, non-empty ffprobe output, expected frame count/fps/duration, zero blackdetect events, one asset hash per character across all cameras, complete `camera_log.json`, `motion_log.json`, and `asset_log.json`.

- [ ] **Step 4: Perform one manual/VLM Proxy review**

Check: male/female identity assignment, full body visibility, hand/foot contact, customer-handle coupling, helper lane, vendor-counter relation, paper timing, and master/lateral/reverse/elevated camera responsibilities. Any scene/character/object/camera/physical rejection returns to Proxy revision; no Appearance-only prompt is generated.

- [ ] **Step 5: Commit the evidence and update docs**

```bash
git add docs/FIXED_ASSET_HUMANOID_RUN_20260810.md docs/CURRENT_PIPELINE_AND_RESULTS.md README.md
git commit -m "record fixed asset humanoid proxy regression"
```

### Task 8: Full regression and handoff

**Files:**
- Modify: `README.md`, `docs/CURRENT_PIPELINE_AND_RESULTS.md`

- [ ] **Step 1: Run targeted tests**

Run: `.venv\Scripts\python.exe -m pytest -q tests/test_asset_catalog.py tests/test_rig_mapping.py tests/test_asset_verifiers.py tests/test_complex_scene_suite.py tests/test_final_video_verifier.py`

Expected: all tests pass; old proxy branches remain covered.

- [ ] **Step 2: Run syntax and diff checks**

Run: `.venv\Scripts\python.exe -m compileall videoactagent scripts pipeline_v2` and `git diff --check`.

Expected: no syntax errors and no whitespace errors.

- [ ] **Step 3: Update the README usage table**

Document `--proxy-style asset_humanoid`, the two required model paths, catalog validation, the explicit `asset_missing` failure, and the fact that Proxy asset consistency does not guarantee Seedance cross-task identity consistency.

- [ ] **Step 4: Commit and push**

```bash
git add README.md docs/CURRENT_PIPELINE_AND_RESULTS.md
git commit -m "document fixed character asset workflow"
git push origin stage0-api-baseline
```

## Self-review

- Spec coverage: catalog and licensing are Tasks 1/6; unified rigs and motion constraints are Tasks 2/4; scene asset IDs are Task 3; shared-world and camera checks are Task 5; compatibility is Task 4; real evidence and documentation are Tasks 7/8.
- Reserved-token scan: clean; missing binary assets fail explicitly as `asset_missing`, and no seed-search step is included.
- Type consistency: all callers use `asset_id`, `trajectory_id`, `AssetCatalog.require()`, `AssetCatalog.materialize()`, and the unified 16-bone names defined in Task 2.
