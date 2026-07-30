"""Cross-stage source-secret scan for all repository ``*.py`` files.

Run: ``& $PY -m unittest tests.test_source_safety -v`` (see
``docs/DEBUGGING.md``). The repository Python sources are the input and the test
produces no artifact. Passing means the scanned key pattern was absent; it is
not a comprehensive secret audit, API test, or model-generation acceptance.
"""

from pathlib import Path
import re
import unittest


class SourceSafetyTests(unittest.TestCase):
    def test_python_sources_contain_no_embedded_pk_key(self):
        pattern = re.compile(r"pk-[A-Za-z0-9-]{20,}")
        offenders = []
        for path in Path(".").rglob("*.py"):
            if pattern.search(path.read_text(encoding="utf-8")):
                offenders.append(str(path))
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
