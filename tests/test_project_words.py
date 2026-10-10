import csv
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import project_words as pw


class ProjectWordsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.project = self.base / "project"
        self.project.mkdir()
        (self.project / "FlowEngine.py").write_text("synthetic source", encoding="utf-8")
        (self.project / "package.json").write_text(json.dumps({"name": "@sample/Flow, Café"}), encoding="utf-8")
        (self.project / "not-selected-secret.txt").write_text("UnselectedOnly", encoding="utf-8")
        self.config = self.base / "config.json"
        self.config.write_text(json.dumps({"root": "project", "files": ["FlowEngine.py"], "manifests": {"package.json": ["name"]}}), encoding="utf-8")
        self.glossary = self.base / "glossary.json"

    def tearDown(self):
        self.temp.cleanup()

    def test_candidates_are_unconfirmed_and_only_from_selected_sources(self):
        candidates = pw.discover(self.config, self.glossary)
        words = {c["word"] for c in candidates}
        self.assertEqual(words, {"Flow", "Engine", "sample", "Café"})
        data = json.loads(self.glossary.read_text(encoding="utf-8"))
        self.assertEqual(data["terms"], [])
        self.assertNotIn("UnselectedOnly", {c["word"] for c in data["candidates"]})
        self.assertTrue(all(c["sources"] for c in data["candidates"]))
        self.assertEqual(pw.tokens("CaféEngine"), ["Café", "Engine"])

    def test_selected_source_cannot_cross_a_symlink_parent(self):
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "HiddenName.py").write_text("synthetic", encoding="utf-8")
        link = self.project / "linked"
        link.symlink_to(outside, target_is_directory=True)
        config = self.base / "linked-config.json"
        config.write_text(json.dumps({"root": "project", "files": ["linked/HiddenName.py"], "manifests": {}}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "crosses a symlink"):
            pw.discover(config, self.glossary)

    def test_manifest_parent_symlink_is_rejected_before_read(self):
        outside = self.base / "external"
        outside.mkdir()
        (outside / "package.json").write_text(json.dumps({"name": "PrivateOutsideName"}), encoding="utf-8")
        (self.project / "linked-manifest").symlink_to(outside, target_is_directory=True)
        config = self.base / "manifest-link-config.json"
        config.write_text(json.dumps({"root": "project", "files": [], "manifests": {"linked-manifest/package.json": ["name"]}}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "crosses a symlink"):
            pw.discover(config, self.glossary)
        self.assertFalse(self.glossary.exists())

    def test_manifest_parent_swap_to_symlink_is_rejected_before_read(self):
        manifests = self.project / "manifests"
        manifests.mkdir()
        (manifests / "package.json").write_text(json.dumps({"name": "OriginalName"}), encoding="utf-8")
        outside = self.base / "outside-manifests"
        outside.mkdir()
        (outside / "package.json").write_text(json.dumps({"name": "PrivateOutsideName"}), encoding="utf-8")
        config = self.base / "raced-manifest-config.json"
        config.write_text(json.dumps({"root": "project", "files": [], "manifests": {"manifests/package.json": ["name"]}}), encoding="utf-8")
        real_open = os.open
        swapped = False

        def swap_then_open(path, flags, *args, **kwargs):
            nonlocal swapped
            if path == "manifests" and kwargs.get("dir_fd") is not None and not swapped:
                swapped = True
                (self.project / "manifests").rename(self.project / "manifests-moved")
                (self.project / "manifests").symlink_to(outside, target_is_directory=True)
            return real_open(path, flags, *args, **kwargs)

        with mock.patch.object(pw.os, "open", side_effect=swap_then_open) as patched_open:
            pw.os.supports_dir_fd.add(patched_open)
            try:
                with self.assertRaises(OSError):
                    pw.discover(config, self.glossary)
            finally:
                pw.os.supports_dir_fd.discard(patched_open)
        self.assertTrue(swapped)
        self.assertFalse(self.glossary.exists())

    def test_manifest_read_fails_closed_without_descriptor_nofollow_support(self):
        with mock.patch.object(pw.os, "O_NOFOLLOW", None):
            with self.assertRaisesRegex(ValueError, "unavailable on this platform"):
                pw.read_manifest(self.project, "package.json")

    def test_unconfirmed_candidates_are_excluded(self):
        pw.discover(self.config, self.glossary)
        data = json.loads(self.glossary.read_text(encoding="utf-8"))
        cspell = pw.cspell_bytes(pw.approved_terms(data))
        wispr = pw.wispr_csv_bytes(pw.approved_terms(data))
        self.assertEqual(cspell, b"")
        self.assertEqual(wispr, b"")

    def test_alias_collision_blocks_two_concepts_case_insensitively(self):
        terms = [
            {"word": "Flow", "aliases": ["Flo"], "confirmed": True, "sources": [{"path": "one.py"}]},
            {"word": "Engine", "aliases": ["fLo"], "confirmed": True, "sources": [{"path": "two.py"}]},
        ]
        with self.assertRaisesRegex(ValueError, "conflicts"):
            pw.validate_aliases(terms)
        term_collision = [
            {"word": "Flow", "aliases": [], "confirmed": True, "sources": [{"path": "one.py"}]},
            {"word": "flow", "aliases": [], "confirmed": True, "sources": [{"path": "two.py"}]},
        ]
        with self.assertRaisesRegex(ValueError, "duplicate normalized term"):
            pw.validate_aliases(term_collision)

    def test_alias_collision_includes_unicode_and_collapsed_whitespace(self):
        cases = [
            ({"word": "Café", "aliases": [], "confirmed": True, "sources": [{"path": "a.py"}]},
             {"word": "Other", "aliases": ["Cafe\u0301"], "confirmed": True, "sources": [{"path": "b.py"}]}),
            ({"word": "First", "aliases": ["New    Name"], "confirmed": True, "sources": [{"path": "a.py"}]},
             {"word": "Second", "aliases": ["new\tname"], "confirmed": True, "sources": [{"path": "b.py"}]}),
            ({"word": "First", "aliases": ["Alias"], "confirmed": True, "sources": [{"path": "a.py"}]},
             {"word": "Second", "aliases": [" aLIAS "], "confirmed": True, "sources": [{"path": "b.py"}]}),
        ]
        for pair in cases:
            with self.subTest(pair=pair), self.assertRaisesRegex(ValueError, "conflicts"):
                pw.validate_aliases(list(pair))

    def test_csv_quotes_comma_unicode_and_embedded_newlines_round_trip(self):
        content = pw.wispr_csv_bytes([{
            "word": "Café",
            "aliases": ['Quoted "word"', "comma,alias", "line\nbreak"],
            "confirmed": True,
            "sources": [{"path": "demo.py"}],
        }]).decode("utf-8")
        rows = list(csv.reader(io.StringIO(content, newline="")))
        self.assertIn(["comma,alias", "Café"], rows)
        self.assertIn(['Quoted "word"', "Café"], rows)
        self.assertIn(["line\nbreak", "Café"], rows)
        self.assertIn(["Café"], rows)
        self.assertIn('"line\nbreak"', content)

    def test_exports_are_byte_identical(self):
        terms = [
            {"word": "Zebra", "aliases": ["Zee"], "confirmed": True, "sources": [{"path": "z.py"}]},
            {"word": "Café", "aliases": [], "confirmed": True, "sources": [{"path": "a.py"}]},
        ]
        self.assertEqual(pw.cspell_bytes(terms), pw.cspell_bytes(list(reversed(terms))))
        self.assertEqual(pw.wispr_csv_bytes(terms), pw.wispr_csv_bytes(list(reversed(terms))))

    def test_confirmed_term_is_kept_with_stale_source(self):
        self.glossary.write_text(json.dumps({
            "schema_version": 1,
            "terms": [{"word": "Legacy", "aliases": [], "confirmed": True, "sources": [{"path": "old/Legacy.py", "kind": "filename", "active": True}]}],
            "candidates": [],
        }), encoding="utf-8")
        pw.discover(self.config, self.glossary)
        data = json.loads(self.glossary.read_text(encoding="utf-8"))
        self.assertEqual(len(data["terms"]), 1)
        source = data["terms"][0]["sources"][0]
        self.assertEqual(source["path"], "old/Legacy.py")
        self.assertIs(source["active"], False)
        self.assertIn(b"Legacy\n", pw.cspell_bytes(pw.approved_terms(data)))

    def test_invalid_wispr_word_preserves_existing_exports(self):
        word = "x" * 61
        self.glossary.write_text(json.dumps({
            "schema_version": 1,
            "terms": [{"word": word, "confirmed": True, "aliases": [],
                       "sources": [{"path": "synthetic.py"}]}],
            "candidates": [],
        }), encoding="utf-8")
        cspell = self.base / "existing" / "words.txt"
        wispr = self.base / "existing" / "wispr.csv"
        cspell.parent.mkdir()
        cspell_before = b"CSpell-SENTINEL-before\n"
        wispr_before = b"Wispr-SENTINEL-before\r\n"
        cspell.write_bytes(cspell_before)
        wispr.write_bytes(wispr_before)

        with self.assertRaisesRegex(ValueError, "Wispr Flow dictionary word exceeds 60"):
            pw.export(self.glossary, cspell, wispr)

        self.assertEqual(cspell.read_bytes(), cspell_before)
        self.assertEqual(wispr.read_bytes(), wispr_before)

    def test_invalid_wispr_word_creates_no_output_directories(self):
        word = "x" * 61
        self.glossary.write_text(json.dumps({
            "schema_version": 1,
            "terms": [{"word": word, "confirmed": True, "aliases": [],
                       "sources": [{"path": "synthetic.py"}]}],
            "candidates": [],
        }), encoding="utf-8")
        cspell = self.base / "new-cspell" / "words.txt"
        wispr = self.base / "new-wispr" / "wispr.csv"

        with self.assertRaisesRegex(ValueError, "Wispr Flow dictionary word exceeds 60"):
            pw.export(self.glossary, cspell, wispr)

        self.assertFalse(cspell.parent.exists())
        self.assertFalse(wispr.parent.exists())

    def test_valid_export_writes_both_formats(self):
        self.glossary.write_text(json.dumps({
            "schema_version": 1,
            "terms": [{"word": "Flow", "confirmed": True, "aliases": ["Flo"],
                       "sources": [{"path": "synthetic.py"}]}],
            "candidates": [],
        }), encoding="utf-8")
        cspell = self.base / "valid" / "words.txt"
        wispr = self.base / "valid" / "wispr.csv"

        pw.export(self.glossary, cspell, wispr)

        self.assertEqual(cspell.read_bytes(), b"Flo\nFlow\n")
        self.assertEqual(wispr.read_bytes(), b"Flow\r\nFlo,Flow\r\n")

    def test_synthetic_manifest_discovery_and_exports(self):
        pw.discover(self.config, self.glossary)
        data = json.loads(self.glossary.read_text(encoding="utf-8"))
        flow = next(c for c in data["candidates"] if c["word"] == "Flow")
        data["terms"] = [{"word": "Flow", "aliases": ["Efflo"], "confirmed": True, "sources": flow["sources"]}]
        self.glossary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        cspell, wispr = self.base / "out" / "words.txt", self.base / "out" / "wispr.csv"
        pw.export(self.glossary, cspell, wispr)
        self.assertEqual(cspell.read_text(encoding="utf-8"), "Efflo\nFlow\n")
        with wispr.open(encoding="utf-8", newline="") as stream:
            self.assertEqual(list(csv.reader(stream)), [["Flow"], ["Efflo", "Flow"]])


if __name__ == "__main__":
    unittest.main()
