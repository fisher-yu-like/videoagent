from pathlib import Path
import unittest


class DirectorMulticamPanelTests(unittest.TestCase):
    def test_independent_multicam_page_contract(self):
        html = Path("static/director_multicam_panel.html").read_text(encoding="utf-8")
        for element_id in (
            "stage-staging", "stage-camera", "stage-result", "target-select",
            "add-object", "save-staging", "approve-staging",
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
        self.assertNotEqual(
            html, Path("static/director_panel.html").read_text(encoding="utf-8")
        )


if __name__ == "__main__":
    unittest.main()
