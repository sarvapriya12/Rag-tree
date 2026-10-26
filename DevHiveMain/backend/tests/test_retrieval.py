import unittest

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.models import FileChunk
from app.rag import _Feature, _vector_id, bm25_scores, query_terms, tokenize
from app.retrieval import (DEPTH_PROFILES, EVIDENCE_FLOOR, EXTRAS_CHARS, HEADER_CHARS, PROMPT_OVERHEAD_TOKENS,
                           TPM_SAFETY, CHARS_PER_TOKEN, budget_windows, chunks_by_path, collapse_duplicates,
                           expand_windows, folder_digests, is_broad, resolve_profile, select_per_folder)
from app.scope import covers, group_key, normalize_scope, scope_clause


class FakeChunk:
    def __init__(self, body):
        self.body = body
        self.embedding_json = "[0.0, 0.0]"


def file_chunk(chunk_id, path, start, end, symbol=None, body=None, segment=0):
    text = body if body is not None else "\n".join(f"line {n}" for n in range(start, end + 1))
    return FileChunk(id=chunk_id, repository_id="r", branch="main", head_sha="a" * 40, path=path,
                     segment=segment, start_line=start, end_line=end, symbol=symbol,
                     body=f"{path}\n{text}", embedding_json="[0.0]")


class TokenizerTest(unittest.TestCase):
    def test_identifiers_split_and_keep_the_whole_name(self):
        tokens = tokenize("cartoonifyImage and remove_pepper_noise")
        for expected in ("cartoonify", "image", "cartoonifyimage", "remove", "pepper", "noise", "removepeppernoise"):
            self.assertIn(expected, tokens)

    def test_question_drops_filler_words(self):
        terms = query_terms("How does the bundle adjustment work?")
        self.assertTrue({"bundle", "adjustment", "work"} <= set(terms))
        self.assertFalse({"how", "does", "the"} & set(terms))

    def test_stemming_and_prefixes_connect_word_forms(self):
        self.assertIn("train", tokenize("trainOCR"))
        self.assertIn("train", query_terms("how is it trained"))
        self.assertTrue({"~detect"} <= set(tokenize("MarkerDetector")) & set(query_terms("which file detects markers")))


class Bm25Test(unittest.TestCase):
    def test_exact_identifier_outranks_prose(self):
        chunks = [FakeChunk("bundle adjustment refines camera poses and points"),
                  FakeChunk("File BundleAdjuster.cpp :: adjustBundle (lines 1-40)\nvoid adjustBundle() { bundle adjustment }"),
                  FakeChunk("number plate recognition with SVM and OCR")]
        features = [_Feature(c) for c in chunks]
        scores = bm25_scores(features, query_terms("where is adjustBundle implemented"))
        self.assertEqual(max(range(3), key=scores.__getitem__), 1)
        self.assertEqual(scores[2], 0.0)

    def test_rare_terms_weigh_more_than_common_ones(self):
        chunks = [FakeChunk("image image image image"), FakeChunk("image triangulation"), FakeChunk("image")]
        scores = bm25_scores([_Feature(c) for c in chunks], ["image", "triangulation"])
        self.assertEqual(max(range(3), key=scores.__getitem__), 1)

    def test_no_terms_means_no_scores(self):
        self.assertEqual(bm25_scores([_Feature(FakeChunk("x"))], []), [0.0])


class QueryTermsTest(unittest.TestCase):
    def test_stopword_prefix_tokens_do_not_leak(self):
        terms = query_terms("Explain the main function and the implementation")
        self.assertFalse([t for t in terms if t.startswith("~expla") or t.startswith("~functi") or t.startswith("~implem")], terms)
        self.assertIn("main", terms)

    def test_real_prefixes_survive(self):
        self.assertIn("~detect", query_terms("which file detects markers"))


class ScopeTest(unittest.TestCase):
    def test_normalize_dedupes_and_drops_children_of_a_selected_parent(self):
        self.assertEqual(normalize_scope(["a/", "a/b.py", "a", "c/d.py"]), ["a", "c/d.py"])
        self.assertEqual(normalize_scope([]), [])

    def test_normalize_rejects_traversal_backslashes_and_too_many_entries(self):
        for bad in ("../x", "a/../b", "a\\b", "a//b", "./a"):
            with self.assertRaises(ValueError, msg=bad):
                normalize_scope([bad])
        with self.assertRaises(ValueError):
            normalize_scope([f"dir{i}" for i in range(51)])

    def test_covers_matches_the_path_and_whole_folders_only(self):
        self.assertTrue(covers("a/b.py", []))
        self.assertTrue(covers("a/b.py", ["a"]))
        self.assertTrue(covers("a/b.py", ["a/b.py"]))
        self.assertFalse(covers("ab/b.py", ["a"]))

    def test_group_key_names_the_folder_below_the_scope(self):
        self.assertEqual(group_key("Chapter3/src/x.cpp", []), "Chapter3")
        self.assertEqual(group_key("README.md", []), "(root)")
        self.assertEqual(group_key("Chapter3/src/x.cpp", ["Chapter3"]), "Chapter3/src")
        self.assertEqual(group_key("Chapter3/x.cpp", ["Chapter3"]), "Chapter3")
        self.assertEqual(group_key("Chapter3/src/x.cpp", ["Chapter3/src/x.cpp"]), "Chapter3/src")

    def test_sql_filter_treats_like_wildcards_literally(self):
        engine = create_engine("sqlite://")
        FileChunk.__table__.create(engine)
        paths = ["a_b/x.py", "axb/x.py", "a_bc/x.py", "50%/x.py", "500/x.py", "a_b.py", "a_b/sub/y.py"]
        with Session(engine) as db:
            db.add_all(file_chunk(i + 1, path, 1, 5) for i, path in enumerate(paths))
            db.commit()

            def found(scope):
                clause = scope_clause(scope)
                return sorted(db.execute(select(FileChunk.path).where(clause)).scalars().all())

            self.assertEqual(found(["a_b"]), ["a_b/sub/y.py", "a_b/x.py"])
            self.assertEqual(found(["50%"]), ["50%/x.py"])
            self.assertEqual(found(["a_b.py"]), ["a_b.py"])
            self.assertEqual(found(["a_b", "500"]), ["500/x.py", "a_b/sub/y.py", "a_b/x.py"])
        self.assertIsNone(scope_clause([]))
        engine.dispose()


class SelectPerFolderTest(unittest.TestCase):
    PROFILE = {"per_folder": 2, "per_file_in_folder": 1}

    def corpus(self, chapter2_score=0.5):
        rows = [file_chunk(1, "Chapter1/a.cpp", 1, 25, "a1"), file_chunk(2, "Chapter1/a.cpp", 23, 47, "a2", segment=1),
                file_chunk(3, "Chapter1/b.cpp", 1, 25, "b1"),
                file_chunk(4, "Chapter2/c.cpp", 1, 25, "c1"), file_chunk(5, "Chapter2/c.cpp", 23, 47, "c2", segment=1),
                file_chunk(6, "Chapter3/README.txt", 1, 5), file_chunk(7, "Chapter3/d.cpp", 1, 25, "d1")]
        scores = {_vector_id(rows[0]): 1.0, _vector_id(rows[1]): 0.9, _vector_id(rows[2]): 0.8,
                  _vector_id(rows[3]): chapter2_score, _vector_id(rows[4]): chapter2_score - 0.05}
        ranked = sorted((r for r in rows if _vector_id(r) in scores), key=lambda r: -scores[_vector_id(r)])
        return rows, ranked, scores

    def test_every_folder_gets_seeds_for_an_overview_question(self):
        rows, ranked, scores = self.corpus()
        seeds, relevance, wanted = select_per_folder(ranked, rows, scores, [], self.PROFILE, broad=True)
        by_folder = {}
        for seed in seeds:
            by_folder.setdefault(group_key(seed.path, []), []).append(seed.path)
        self.assertEqual(wanted, {"Chapter1", "Chapter2", "Chapter3"})
        self.assertEqual(by_folder["Chapter1"], ["Chapter1/a.cpp", "Chapter1/b.cpp"])  # one per file in a busy folder
        self.assertEqual(by_folder["Chapter2"], ["Chapter2/c.cpp", "Chapter2/c.cpp"])  # a one-file folder may repeat
        # An unmatched folder: its README first, then the opening of another file, up to per_folder.
        self.assertEqual(by_folder["Chapter3"], ["Chapter3/README.txt", "Chapter3/d.cpp"])
        self.assertEqual(relevance["Chapter3"], 0.0)

    def test_pointed_questions_keep_to_matching_folders(self):
        rows, ranked, scores = self.corpus(chapter2_score=0.2)
        seeds, _, wanted = select_per_folder(ranked, rows, scores, [], self.PROFILE, broad=False)
        self.assertEqual(wanted, {"Chapter1"})
        self.assertEqual({group_key(s.path, []) for s in seeds}, {"Chapter1"})
        rows, ranked, scores = self.corpus(chapter2_score=0.5)
        self.assertEqual(select_per_folder(ranked, rows, scores, [], self.PROFILE, broad=False)[2],
                         {"Chapter1", "Chapter2"})

    def test_when_no_code_matches_only_overviews_get_representatives(self):
        rows, _, _ = self.corpus()
        seeds, _, wanted = select_per_folder([], rows, {}, [], self.PROFILE, broad=False)
        self.assertEqual((seeds, wanted), ([], set()))  # e.g. a history question: digests only
        seeds, _, wanted = select_per_folder([], rows, {}, [], self.PROFILE, broad=True)
        self.assertEqual(wanted, {"Chapter1", "Chapter2", "Chapter3"})
        # Openings of up to two files per folder: a.cpp + b.cpp, c.cpp, README.txt + d.cpp.
        self.assertEqual(sorted(s.path for s in seeds), ["Chapter1/a.cpp", "Chapter1/b.cpp", "Chapter2/c.cpp",
                                                         "Chapter3/README.txt", "Chapter3/d.cpp"])

    def test_more_depth_picks_more_sections_per_folder(self):
        rows, ranked, scores = self.corpus()
        low = select_per_folder(ranked, rows, scores, [], {"per_folder": 1, "per_file_in_folder": 1}, True)[0]
        high = select_per_folder(ranked, rows, scores, [], {"per_folder": 3, "per_file_in_folder": 2}, True)[0]
        self.assertEqual(len(low), 3)
        self.assertGreater(len(high), len(low))


class DuplicateTest(unittest.TestCase):
    def test_same_file_in_two_chapters_collapses_to_the_best_copy(self):
        body = "void blur(Mat m) { int a = 1; int b = 2; int c = 3; filter(m, a, b, c); }"
        first = file_chunk(1, "Chapter1/ImageUtils.cpp", 10, 30, "blur", body)
        copy = file_chunk(2, "Chapter8/ImageUtils.cpp", 12, 32, "blur", body)
        other = file_chunk(3, "Chapter8/Other.cpp", 1, 9, "blur", body)
        kept, duplicates = collapse_duplicates([first, copy, other])
        self.assertEqual([c.path for c in kept], ["Chapter1/ImageUtils.cpp", "Chapter8/Other.cpp"])
        self.assertEqual(duplicates, {_vector_id(first): ["Chapter8/ImageUtils.cpp"]})

    def test_different_code_with_the_same_name_is_kept(self):
        one = file_chunk(1, "a/Utils.cpp", 1, 5, "run", "alpha beta gamma delta epsilon")
        two = file_chunk(2, "b/Utils.cpp", 1, 5, "run", "completely unrelated tokens appear here")
        self.assertEqual(len(collapse_duplicates([one, two])[0]), 2)


class ExpansionTest(unittest.TestCase):
    def chunks(self):
        # 25-line chunks overlapping by 3 lines: 1-25, 23-47, 45-69, 67-91.
        return [file_chunk(i + 1, "src/big.cpp", 1 + i * 22, 25 + i * 22, segment=i) for i in range(4)]

    def test_window_rebuilds_the_exact_source_lines_without_repeats(self):
        rows = self.chunks()
        scores = {_vector_id(rows[0]): 0.9}  # prefer the earlier neighbour first, then the later one
        window = expand_windows([rows[1]], chunks_by_path(rows), scores, 60, [])[0]
        self.assertEqual((window["start_line"], window["end_line"]), (1, 69))
        self.assertEqual(window["code"].split("\n"), [f"line {n}" for n in range(1, 70)])
        self.assertEqual(window["sections"], 3)

    def test_neighbours_follow_their_scores(self):
        rows = self.chunks()
        scores = {_vector_id(rows[2]): 0.9, _vector_id(rows[0]): 0.1}
        window = expand_windows([rows[1]], chunks_by_path(rows), scores, 50, [])[0]
        self.assertEqual((window["start_line"], window["end_line"]), (23, 69))

    def test_ties_grow_towards_the_following_code(self):
        rows = self.chunks()
        window = expand_windows([rows[1]], chunks_by_path(rows), {}, 60, [])[0]
        self.assertEqual((window["start_line"], window["end_line"]), (23, 91))

    def test_touching_seeds_merge_into_one_window(self):
        rows = self.chunks()
        windows = expand_windows([rows[0], rows[3]], chunks_by_path(rows), {}, 25, [])
        self.assertEqual(len(windows), 2)
        merged = expand_windows([rows[0], rows[1]], chunks_by_path(rows), {}, 25, [])
        self.assertEqual(len(merged), 1)
        self.assertEqual((merged[0]["start_line"], merged[0]["end_line"]), (1, 47))
        self.assertEqual(merged[0]["seeds"], 2)

    def test_window_carries_group_and_duplicates(self):
        rows = [file_chunk(1, "Chapter2/a.cpp", 1, 20, "f")]
        window = expand_windows(rows, chunks_by_path(rows), {_vector_id(rows[0]): 0.5}, 25, [],
                                {_vector_id(rows[0]): ["Chapter8/a.cpp"]})[0]
        self.assertEqual((window["group"], window["duplicates"], window["score"]), ("Chapter2", ["Chapter8/a.cpp"], 0.5))


def big_window(index, group, score, lines=80):
    code = "\n".join(f"    int value_{index}_{n} = compute({n});  // step" for n in range(lines))
    return {"path": f"{group}/file{index}.cpp", "group": group, "score": score, "start_line": 1, "end_line": lines,
            "focus_start": 1, "focus_end": 25, "code": code}


class DigestTest(unittest.TestCase):
    def test_digests_name_every_folder_with_key_functions_and_readme(self):
        rows = [file_chunk(1, "README.md", 1, 3, body="Project overview text"),
                file_chunk(2, "Chapter1/README.txt", 1, 3, body="Cartoonifier on Android"),
                file_chunk(3, "Chapter1/a.cpp", 1, 9, "cartoonifyImage"), file_chunk(4, "Chapter2/b.cpp", 1, 9, "solve")]
        profile = resolve_profile("low", 7000)
        digest = folder_digests(rows, [], {"Chapter1": 0.9}, {_vector_id(rows[2]): 0.9}, profile)
        by_name = {row["path"]: row for row in digest["folders"]}
        self.assertEqual(set(by_name), {"(root)", "Chapter1", "Chapter2"})
        self.assertEqual((by_name["Chapter1"]["files"], by_name["Chapter1"]["chunks"]), (2, 2))
        self.assertEqual(by_name["Chapter1"]["symbols"], ["cartoonifyImage"])
        self.assertIn("Cartoonifier", by_name["Chapter1"]["readme"])
        self.assertEqual(by_name["Chapter1"]["relevance"], 0.9)
        for name in by_name:
            self.assertIn(name, digest["text"])
        self.assertIsNone(folder_digests([], [], {}, {}, profile))

    def test_readme_banners_are_dropped_from_the_digest(self):
        banner = "/" + "*" * 70 + "\n * Fluid Interaction using Kinect\n " + "*" * 70 + "/\nBuild with CMake."
        rows = [file_chunk(1, "Chapter9/README.txt", 1, 4, body=banner)]
        digest = folder_digests(rows, [], {}, {}, resolve_profile("medium", 7000))
        self.assertEqual(digest["folders"][0]["readme"], "Fluid Interaction using Kinect Build with CMake.")

    def test_many_folders_still_appear_by_name(self):
        rows = [file_chunk(i, f"pkg{i:03d}/x.cpp", 1, 5, f"fn{i}") for i in range(1, 301)]
        profile = resolve_profile("low", 7000)
        digest = folder_digests(rows, [], {}, {}, profile)
        self.assertTrue(all(f"pkg{i:03d}" in digest["text"] for i in range(1, 301)))


class BudgetTest(unittest.TestCase):
    def per_folder_code(self, depth, folders=10):
        profile = resolve_profile(depth, 7000)
        windows = [big_window(f * 10 + w, f"Chapter{f}", 1.0 - f * 0.05 - w * 0.01)
                   for f in range(folders) for w in range(profile["per_folder"])]
        relevance = {f"Chapter{f}": 1.0 - f * 0.05 for f in range(folders)}
        kept = budget_windows(windows, relevance, profile, profile["digest_chars"])
        code = {}
        for window in kept:
            code[window["group"]] = code.get(window["group"], 0) + len(window["excerpt"])
        return profile, kept, code

    def test_every_folder_keeps_code_and_the_budget_holds(self):
        for depth in ("low", "medium", "high", "very_high"):
            profile, kept, code = self.per_folder_code(depth)
            self.assertEqual(len(code), 10, depth)
            spent = sum(len(w["excerpt"]) + HEADER_CHARS + (EXTRAS_CHARS[profile["extras"]] if w["extras"] else 0)
                        for w in kept)
            self.assertLessEqual(spent, profile["evidence_chars"] - profile["digest_chars"] - profile["tail_chars"], depth)

    def test_deeper_levels_send_more_code_per_folder(self):
        averages = []
        for depth in ("low", "medium", "high", "very_high"):
            _, _, code = self.per_folder_code(depth)
            averages.append(sum(code.values()) / len(code))
        self.assertEqual(averages, sorted(averages))
        self.assertEqual(len(set(averages)), 4, averages)

    def test_deeper_levels_send_more_sections_when_the_budget_allows(self):
        _, low, _ = self.per_folder_code("low", folders=3)
        _, very_high, _ = self.per_folder_code("very_high", folders=3)
        self.assertEqual(len(low), 3)  # one section per folder
        self.assertGreater(len(very_high), 6)  # several sections per folder

    def test_relevant_folders_get_more_than_weak_ones(self):
        _, _, code = self.per_folder_code("high")
        self.assertGreater(code["Chapter0"], code["Chapter9"])

    def test_overviews_share_the_budget_almost_evenly(self):
        profile = resolve_profile("high", 7000)
        windows = [big_window(f, f"Chapter{f}", 1.0 if f == 0 else 0.0) for f in range(5)]
        relevance = {f"Chapter{f}": 1.0 if f == 0 else 0.0 for f in range(5)}
        pointed = {w["group"]: len(w["excerpt"]) for w in budget_windows([dict(w) for w in windows], relevance,
                                                                         profile, 500, broad=False)}
        overview = {w["group"]: len(w["excerpt"]) for w in budget_windows([dict(w) for w in windows], relevance,
                                                                          profile, 500, broad=True)}
        self.assertGreater(pointed["Chapter0"] / pointed["Chapter4"], overview["Chapter0"] / overview["Chapter4"])
        self.assertLess(overview["Chapter0"] / overview["Chapter4"], 1.6)

    def test_too_many_folders_keep_code_for_the_most_relevant(self):
        profile, kept, code = self.per_folder_code("low", folders=40)
        self.assertLess(len(code), 40)
        self.assertIn("Chapter0", code)
        self.assertNotIn("Chapter39", code)
        self.assertEqual([w["group"] for w in kept], sorted({w["group"] for w in kept},
                         key=lambda g: int(g[7:])))  # most relevant first

class BroadTest(unittest.TestCase):
    def test_broad_questions(self):
        self.assertTrue(is_broad("What does each chapter do?", [], "low"))
        self.assertTrue(is_broad("Where is the parser?", ["a", "b"], "low"))
        self.assertTrue(is_broad("Where is the parser?", [], "very_high"))
        self.assertFalse(is_broad("Where is adjustBundle implemented?", [], "medium"))


class ProfileBudgetTest(unittest.TestCase):
    def test_one_call_never_exceeds_the_token_budget(self):
        for tpm in (1000, 4000, 7000, 8000, 30000):
            for depth in DEPTH_PROFILES:
                profile = resolve_profile(depth, tpm)
                usable = tpm * TPM_SAFETY
                pairs = [(profile["map_chars"], profile["completion_tokens"]),
                         (profile["reduce_chars"], profile["reduce_tokens"])] if profile["map_reduce"] else \
                        [(profile["evidence_chars"], profile["completion_tokens"])]
                for chars, completion in pairs:
                    spent = PROMPT_OVERHEAD_TOKENS + chars / CHARS_PER_TOKEN + completion
                    self.assertTrue(spent <= usable or chars == EVIDENCE_FLOOR, (tpm, depth, spent, usable))

    def test_depth_grows_evidence_at_the_default_budget(self):
        sizes = [resolve_profile(depth, 7000)["evidence_chars"] for depth in ("low", "medium", "high")]
        self.assertEqual(sizes, sorted(sizes))
        self.assertLess(sizes[0], sizes[2])
        self.assertTrue(resolve_profile("very_high", 7000)["map_reduce"])


if __name__ == "__main__":
    unittest.main()
