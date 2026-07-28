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
