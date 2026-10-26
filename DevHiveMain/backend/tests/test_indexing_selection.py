import unittest

from app.indexing import _select_files


class IndexSelectionTest(unittest.TestCase):
    def test_partial_snapshot_keeps_implementation_docs_and_tests(self):
        files = ([{"path": f"tests/test_case_{i:02d}.py"} for i in range(20)] +
                 [{"path": f"docs/topic_{i:02d}.md"} for i in range(20)] +
                 [{"path": f"src/service_{i:02d}.py"} for i in range(50)])
        selected = _select_files(files)
        paths = [item["path"] for item in selected]
        self.assertEqual(len(paths), 60)
        self.assertEqual(len(set(paths)), 60)
        self.assertEqual(sum(path.startswith("src/") for path in paths), 45)
        self.assertEqual(sum(path.startswith("docs/") for path in paths), 10)
        self.assertEqual(sum(path.startswith("tests/") for path in paths), 5)


if __name__ == "__main__":
    unittest.main()
