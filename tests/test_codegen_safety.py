from __future__ import annotations

import unittest

from videoactagent.codegen_safety import CodegenSafetyError, validate_generated_code


SAFE = """import bpy
import math
from mathutils import Vector
SCENE_VERSION = 1
def build_scene(context):
    bpy.ops.mesh.primitive_cube_add(location=(0, 0, 0))
    return Vector((math.sin(0), 0, 0))
"""


class CodegenSafetyTests(unittest.TestCase):
    def test_accepts_safe_build_scene(self):
        value = validate_generated_code(SAFE)
        self.assertEqual(value["status"], "accepted")
        self.assertEqual(value["entrypoint"], "build_scene")
        self.assertEqual(value["imports"], ["bpy", "math", "mathutils"])
        self.assertEqual(len(value["sha256"]), 64)

    def test_rejects_blocked_operations(self):
        blocked = {
            "import os\ndef build_scene(context): pass": "import os",
            "def build_scene(context): open('x', 'w')": "open",
            "def build_scene(context): exec('x=1')": "exec",
            "import bpy\ndef build_scene(context): bpy.ops.wm.open_mainfile(filepath='x')": "bpy.ops.wm",
            "import bpy\ndef build_scene(context): bpy.ops.render.render(animation=True)": "bpy.ops.render",
            "import bpy\nbpy.ops.mesh.primitive_cube_add()\ndef build_scene(context): pass": "top-level",
        }
        for code, text in blocked.items():
            with self.subTest(text=text), self.assertRaisesRegex(CodegenSafetyError, text):
                validate_generated_code(code)

    def test_rejects_shape_bypasses(self):
        cases = [
            ("def build_scene(): pass", "context"),
            ("def build_scene(context): pass\ndef build_scene(context): pass", "exactly one"),
            ("import importlib\ndef build_scene(context): pass", "import"),
            ("from os import path\ndef build_scene(context): pass", "import"),
            ("def build_scene(context): __import__('os')", "__import__"),
            ("def build_scene(context): eval('1')", "eval"),
            ("x = open\ndef build_scene(context): pass", "assignment"),
        ]
        for code, text in cases:
            with self.subTest(text=text), self.assertRaises(CodegenSafetyError):
                validate_generated_code(code)

    def test_rejects_large_code(self):
        code = "def build_scene(context):\n    x = '" + ("a" * 40960) + "'\n"
        with self.assertRaisesRegex(CodegenSafetyError, "40 KiB"):
            validate_generated_code(code)


if __name__ == "__main__":
    unittest.main()
