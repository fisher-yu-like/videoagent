import unittest


class JDResponseParsingTests(unittest.TestCase):
    """Controlled parser tests; these dictionaries are not gateway evidence."""

    def test_extracts_task_id_from_inherited_gateway_shape(self):
        from videoactagent.backends.jd import extract_task_id

        self.assertEqual(
            extract_task_id({"result": {"task_id": "controlled-parser-id"}}),
            "controlled-parser-id",
        )
        self.assertEqual(
            extract_task_id({"task_id": "controlled-root-id"}),
            "controlled-root-id",
        )

    def test_extracts_status_from_both_inherited_shapes(self):
        from videoactagent.backends.jd import extract_status

        self.assertEqual(extract_status({"task_status": "success"}), "success")
        self.assertEqual(
            extract_status({"result": {"task_status": "failed"}}),
            "failed",
        )
        self.assertEqual(extract_status({"status": "processing"}), "processing")

    def test_extracts_video_urls_from_root_or_result_content(self):
        from videoactagent.backends.jd import extract_video_urls

        root = {
            "content": [
                {
                    "id": "controlled-video",
                    "video_url": {"url": "https://unit.invalid/root.mp4"},
                }
            ]
        }
        nested = {
            "result": {
                "content": [
                    {"video_url": {"url": "https://unit.invalid/nested.mp4"}}
                ]
            }
        }
        self.assertEqual(
            extract_video_urls(root),
            [("controlled-video", "https://unit.invalid/root.mp4")],
        )
        self.assertEqual(
            extract_video_urls(nested),
            [("video", "https://unit.invalid/nested.mp4")],
        )

    def test_rejects_missing_task_id(self):
        from videoactagent.backends.jd import extract_task_id

        with self.assertRaisesRegex(ValueError, "task_id"):
            extract_task_id({"result": {}})


if __name__ == "__main__":
    unittest.main()
