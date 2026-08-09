# ACCAD BVH motion clips

These files are real motion-capture inputs for the optional `revision_021`
proxy branch.  The Blender sandbox imports the BVH armature as a hidden source
rig, retargets local bone rotations onto the shared CesiumMan humanoid, and
keeps the authored WorldState root trajectory and all camera trajectories
unchanged.

Source: [ACCAD Motion Lab / Open Motion Project](https://accad.osu.edu/research/motion-lab/mocap-system-and-data)

The ACCAD page identifies the Open Motion Project data as **CC BY 3.0**.  The
original archive was downloaded from:

`https://accad.osu.edu/sites/accad.osu.edu/files/Female1_bvh.zip`

Tracked files and SHA-256:

| File | SHA-256 |
|---|---|
| `Female1_C24_SideStepLeft.bvh` | `B77616FD2FD3CD26686F228200D40A182690905F12F0DE9645FBCCE5DE45D254` |
| `Female1_C25_SideStepRight.bvh` | `BFC871E41354B7F4BC31A1057D086ABCEF9AD121AC8C138EDFA1B4C775C1D7FA` |
| `Female1_D3_ConversationGestures.bvh` | `not used by the current run; available for a later gesture ablation` |
| `Female1_bvh.zip` | `84543297A24FE9900449675837B5B7758C5208D0F56B699D10A3EB362A46530A` |

The files are not a training dataset for this repository.  They are an
auditable real-motion probe used to compare procedural side-step keyframes
against BVH retargeting without changing the upstream scene or camera plans.
