"""Small dispatcher for the installed ``videoactagent`` console command."""

from __future__ import annotations

import importlib
import sys
from typing import Sequence


COMMANDS = {
    "baseline-audit": "videoactagent.baseline_audit",
    "camera-eval": "videoactagent.camera_eval",
    "closed-loop": "videoactagent.closed_loop",
    "control-bridge": "videoactagent.control_bridge",
    "director-loop": "videoactagent.director_loop",
    "jd-smoke": "videoactagent.jd_smoke",
    "module-io": "videoactagent.module_io",
    "annotate": "videoactagent.manual_annotation",
    "stage3-audit": "videoactagent.stage3_audit",
    "stage6-debug": "videoactagent.stage6_debug",
    "trajectory": "videoactagent.trajectory",
    "trajectory-author": "videoactagent.trajectory_author",
    "trajectory-backend": "videoactagent.trajectory_backend",
    "trajectory-compile": "videoactagent.trajectory_compile",
    "trajectory-editor": "videoactagent.trajectory_editor",
    "trajectory-eval": "videoactagent.trajectory_eval",
    "trajectory-experiment": "videoactagent.trajectory_experiment",
    "trajectory-observe": "videoactagent.trajectory_observe",
    "trajectory-proxy": "videoactagent.trajectory_proxy",
    "vace-inputs": "videoactagent.vace_inputs",
    "vace-coded-draft": "videoactagent.vace_coded_draft",
    "vace-probe": "videoactagent.vace_preprocess_probe",
}


def _usage() -> str:
    commands = "\n".join(f"  {name}" for name in sorted(COMMANDS))
    return "Usage: videoactagent <command> [module options]\n\nCommands:\n" + commands


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help"}:
        print(_usage())
        return 0
    command = args.pop(0)
    module_name = COMMANDS.get(command)
    if module_name is None:
        print(f"unknown command: {command}\n\n{_usage()}", file=sys.stderr)
        return 2
    module = importlib.import_module(module_name)
    entrypoint = getattr(module, "main", None)
    if not callable(entrypoint):
        raise RuntimeError(f"module has no CLI main(): {module_name}")
    try:
        result = entrypoint(args)
    except SystemExit as exc:
        # argparse-based module CLIs use SystemExit for --help and parse errors.
        return int(exc.code) if isinstance(exc.code, int) else 1
    return int(result) if isinstance(result, int) else 0


if __name__ == "__main__":
    raise SystemExit(main())
