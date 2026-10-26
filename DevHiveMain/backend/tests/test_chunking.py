import unittest

from app.chunking import HARD_CHARS, MAX_LINES, OVERLAP, TARGET_LINES, chunk_source
from app.file_graph import dependencies
from app.indexing import _vendored, _is_text_path


CPP = "\n".join([
    '#include "cartoon.h"',
    "#include <opencv2/core.hpp>",
    "",
    "using namespace cv;",
    "",
    "void cartoonifyImage(Mat srcColor, Mat dst)",
    "{",
    *[f"    int step{i} = {i};" for i in range(70)],
    "}",
    "",
    "void removePepperNoise(Mat &mask)",
    "{",
    *[f"    mask.at<uchar>({i}) = 0;" for i in range(30)],
    "}",
])


class ChunkingTest(unittest.TestCase):
    def test_line_ranges_reproduce_the_file_exactly(self):
        lines = CPP.splitlines()
        chunks = chunk_source("src/cartoon.cpp", CPP)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertEqual(chunk.text, "\n".join(lines[chunk.start_line - 1:chunk.end_line]))
        self.assertEqual(chunks[0].start_line, 1)
        self.assertEqual(chunks[-1].end_line, len(lines))

    def test_every_line_is_covered_and_symbols_are_named(self):
        chunks = chunk_source("src/cartoon.cpp", CPP)
        covered = set()
        for chunk in chunks:
            covered.update(range(chunk.start_line, chunk.end_line + 1))
        self.assertEqual(covered, set(range(1, len(CPP.splitlines()) + 1)))
        self.assertIn("cartoonifyImage", [c.symbol for c in chunks])
        self.assertIn("removePepperNoise", [c.symbol for c in chunks])

    def test_large_file_is_not_truncated(self):
        big = "\n".join(f"int value_{i} = {i};" for i in range(4000))
        chunks = chunk_source("big.cpp", big)
        self.assertEqual(chunks[-1].end_line, 4000)

    def test_python_definitions_become_symbols(self):
        source = "import os\n\n\ndef load():\n    return 1\n\n\nclass Store:\n    pass\n"
        self.assertEqual(chunk_source("a.py", source)[0].symbol, "load")

    def test_empty_file_has_no_chunks(self):
        self.assertEqual(chunk_source("x.cpp", ""), [])

    def test_chunks_stay_small_for_precise_retrieval(self):
        chunks = chunk_source("src/cartoon.cpp", CPP)
        for chunk in chunks:
            self.assertLessEqual(chunk.end_line - chunk.start_line + 1, MAX_LINES)
            self.assertLessEqual(len(chunk.text), HARD_CHARS + 200)
        average = sum(c.end_line - c.start_line + 1 for c in chunks) / len(chunks)
        self.assertLessEqual(average, TARGET_LINES + 8)

    def test_neighbouring_chunks_overlap_by_at_most_the_overlap(self):
        chunks = chunk_source("src/cartoon.cpp", CPP)
        for before, after in zip(chunks, chunks[1:]):
            self.assertLessEqual(max(0, before.end_line - after.start_line + 1), OVERLAP)
            self.assertGreater(after.start_line, before.start_line)


class IncludeGraphTest(unittest.TestCase):
    PATHS = {"ch4/Main.cpp", "ch4/Common.h", "ch4/sub/Util.h", "ch8/Util.h", "ch8/main.cpp"}

    def test_quoted_include_resolves_next_to_includer(self):
        found = dependencies("ch4/Main.cpp", '#include "Common.h"\n#include <vector>\n', self.PATHS)
        self.assertEqual(found, {"ch4/Common.h"})

    def test_system_includes_are_ignored(self):
        self.assertEqual(dependencies("ch4/Main.cpp", "#include <opencv2/core.hpp>\n", self.PATHS), set())

    def test_subfolder_and_basename_fallback_prefer_nearest(self):
        self.assertEqual(dependencies("ch4/Main.cpp", '#include "sub/Util.h"', self.PATHS), {"ch4/sub/Util.h"})
        self.assertEqual(dependencies("ch8/main.cpp", '#include "Util.h"', self.PATHS), {"ch8/Util.h"})

    def test_objc_import(self):
        self.assertEqual(dependencies("ch4/Main.cpp", '#import "Common.h"', self.PATHS), {"ch4/Common.h"})


class IndexingFilterTest(unittest.TestCase):
    def test_vendored_frameworks_are_skipped(self):
        self.assertTrue(_vendored("Chapter2_iPhoneAR/opencv2.framework/Versions/A/Headers/core.hpp"))
        self.assertTrue(_vendored("app/Pods/Foo/foo.m"))
        self.assertFalse(_vendored("Chapter4_StructureFromMotion/BundleAdjuster.cpp"))

    def test_c_family_and_cmake_are_text(self):
        for path in ("a/b.cpp", "a/b.hpp", "a/b.mm", "a/CMakeLists.txt", "a/Makefile"):
            self.assertTrue(_is_text_path(path), path)
        self.assertFalse(_is_text_path("a/model.xml"))


if __name__ == "__main__":
    unittest.main()
