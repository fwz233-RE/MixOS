"""Font construction tests; default suite creates its own small TrueType font.

Opt in to real-font integration with MIXOS_TEST_FONT=D:\\AI\\MiSans-Normal.ttf.
No board, network, compiler or proprietary font is needed for ordinary tests.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("build_font", ROOT / "tools/build_font.py")
font = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(font)


def synthetic_ttf(path: Path, points: set[int], *, empty: set[int] = frozenset()) -> None:
    builder = FontBuilder(1000, isTTF=True)
    cmap = {cp: f"uni{cp:04X}" for cp in sorted(points)}
    order = [".notdef", *cmap.values()]
    builder.setupGlyphOrder(order)
    builder.setupCharacterMap(cmap)
    glyphs = {}
    for name in order:
        pen = TTGlyphPen(None)
        cp = int(name[3:], 16) if name != ".notdef" else 0
        if cp not in empty and not chr(cp).isspace():
            pen.moveTo((50, 0))
            pen.lineTo((550, 0))
            pen.lineTo((550, 700))
            pen.lineTo((50, 700))
            pen.closePath()
        glyphs[name] = pen.glyph()
    builder.setupGlyf(glyphs)
    builder.setupHorizontalMetrics({name: (600, 50) for name in order})
    builder.setupHorizontalHeader(ascent=800, descent=-200)
    builder.setupNameTable({"familyName": "MixOS Synthetic Test", "styleName": "Regular",
                           "uniqueFontIdentifier": "MixOS-Synthetic-1", "fullName": "MixOS Synthetic Test Regular",
                           "psName": "MixOS-Synthetic-Test-Regular", "version": "Version 1.0"})
    builder.setupOS2(sTypoAscender=800, sTypoDescender=-200, usWinAscent=800, usWinDescent=200)
    builder.setupPost()
    builder.setupMaxp()
    builder.save(path)


class ExtractionTests(unittest.TestCase):
    def test_actual_ui_all_chinese_literals(self):
        points, literals = font.extract_ui(font.DEFAULT_UI.read_text(encoding="utf-8"))
        self.assertGreater(len(literals), 200)
        self.assertTrue(set(map(ord, "石墨薄荷纸白森林午夜蓝暖琥珀批准待处理的固件更新")) <= points)
        self.assertTrue(set(map(ord, "℃✓✕")) <= set(map(ord, font.EXTRA_TEXT)))

    def test_comments_arrays_branches_concatenation_and_escapes(self):
        source = r'''// "假"
        /* "骗" */ char quote='"';
        const char *a[]={"首页", u8"设" "置", "\u4E2D\U00006587"};
        draw(flag ? "电池" : "键盘");
        draw("\xE7\xBB\x88\347\253\257\n\t\"\\");
        '''
        points, literals = font.extract_ui(source)
        self.assertTrue(set(map(ord, '首页设置中文电池键盘终端"\\')) <= points)
        self.assertFalse(set(map(ord, "假骗")) & points)
        self.assertNotIn(10, points)
        self.assertEqual(len(literals), 7)

    def test_line_splicing(self):
        points, _ = font.extract_ui('draw("中\\\n文"); // "假"\\\n"骗"\n')
        self.assertEqual(points, set(map(ord, "中文")))

    def test_reject_unknown_or_malformed_literals(self):
        for source in ('"\\q"', '"unterminated', 'L"中"', '/* unterminated',
                       '"\\x100"', '"\\777"', '"\\uNOPE"', 'int x;'):
            with self.subTest(source=source), self.assertRaises((font.FontBuildError, UnicodeError)):
                font.extract_ui(source)

    def test_gb2312_inventory(self):
        points = font.gb2312_codepoints()
        self.assertEqual(len(points), 7445)
        self.assertIn(0x30FB, points)
        self.assertTrue(set(map(ord, "中文龟")) <= points)


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source.ttf"
        self.output = self.root / "result.ttf"
        self.manifest = self.root / "result.json"
        self.ui = self.root / "mix_ui.c"
        self.ui.write_text('draw("中文");', encoding="utf-8")
        self.points = font.BASIC_LATIN | set(map(ord, font.EXTRA_TEXT + "中文")) | font.RUNTIME_REQUIRED
        synthetic_ttf(self.source, self.points)
        self.original = self.source.read_bytes()

    def build(self):
        return font.build(self.source, self.output, self.manifest, ui_source=self.ui)

    def assert_clean_failure(self):
        self.assertFalse(self.output.exists())
        self.assertFalse(self.manifest.exists())
        self.assertFalse(list(self.root.glob("*.tmp")))
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_success_reproducible_and_source_unchanged(self):
        first = self.build()
        data = self.output.read_bytes()
        manifest = self.manifest.read_bytes()
        # A different clock must not leak into head.created/head.modified.
        with mock.patch("fontTools.ttLib.tables._h_e_a_d.timestampNow", return_value=4000000000):
            second = self.build()
        self.assertEqual(first["output_sha256"], second["output_sha256"])
        self.assertEqual(data, self.output.read_bytes())
        self.assertEqual(manifest, self.manifest.read_bytes())
        self.assertEqual(data[:4], b"\x00\x01\x00\x00")
        self.assertEqual(self.source.read_bytes(), self.original)
        self.assertEqual(first["required_coverage_missing"], [])
        self.assertEqual(first["ui_coverage_missing"], [])
        self.assertEqual(first["load_verification"]["partition_padded_load"], "passed")
        with TTFont(self.output, recalcTimestamp=False) as result:
            self.assertEqual(result["head"].created, font.FIXED_SFNT_TIME)
            self.assertEqual(result["head"].modified, font.FIXED_SFNT_TIME)

    def test_all_pairwise_path_collisions_preserve_every_file(self):
        self.output.write_bytes(b"old output")
        self.manifest.write_bytes(b"old manifest")
        paths = [self.source, self.output, self.manifest, self.ui]
        before = {p: p.read_bytes() for p in paths}
        for a in range(4):
            for b in range(a + 1, 4):
                args = paths.copy()
                args[b] = args[a]
                with self.subTest(a=a, b=b), self.assertRaises(font.FontBuildError):
                    font.build(*args[:3], ui_source=args[3])
                self.assertEqual({p: p.read_bytes() for p in paths}, before)

    def test_normalized_path_collision(self):
        with self.assertRaises(font.FontBuildError):
            font.build(self.source, self.root / "unused/../source.ttf", self.manifest, ui_source=self.ui)
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_hardlink_collision(self):
        try:
            os.link(self.source, self.output)
        except OSError as exc:
            self.skipTest(f"hard links unavailable: {exc}")
        with self.assertRaises(font.FontBuildError):
            self.build()
        self.assertEqual(self.source.read_bytes(), self.original)
        self.assertEqual(self.output.read_bytes(), self.original)

    def test_symlink_collision(self):
        try:
            self.output.symlink_to(self.source)
        except OSError as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        with self.assertRaises(font.FontBuildError):
            self.build()
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_parent_child_destination_collision(self):
        with self.assertRaises(font.FontBuildError):
            font.build(self.source, self.output, self.output / "manifest.json", ui_source=self.ui)
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_missing_required_ui_removes_stale_artifacts(self):
        self.build()
        self.ui.write_text('draw("中文龘");', encoding="utf-8")
        with self.assertRaisesRegex(font.FontBuildError, "U\\+9F98"):
            self.build()
        self.assert_clean_failure()

    def test_missing_required_symbol_is_not_silently_dropped(self):
        synthetic_ttf(self.source, self.points - {ord("✓")})
        self.original = self.source.read_bytes()
        with self.assertRaisesRegex(font.FontBuildError, "U\\+2713"):
            self.build()
        self.assert_clean_failure()

    def test_replacement_alias_is_recorded_and_only_in_subset(self):
        synthetic_ttf(self.source, self.points - {0xFFFD})
        self.original = self.source.read_bytes()
        record = self.build()
        self.assertEqual(record["glyph_aliases"][0]["codepoint"], "U+FFFD")
        with TTFont(self.output) as result:
            self.assertEqual(result.getBestCmap()[0xFFFD], result.getBestCmap()[ord("?")])
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_optional_missing_points_are_enumerated(self):
        record = self.build()
        missing = {row["codepoint"] for row in record["omitted_optional"]}
        self.assertIn("U+30FB", missing)
        self.assertEqual(record["source_missing_optional_codepoints"], len(missing))

    def test_oversized_output_removes_stale_artifacts(self):
        self.build()
        with mock.patch.object(font, "FONT_PARTITION_BYTES", 100), self.assertRaisesRegex(font.FontBuildError, "partition limit"):
            self.build()
        self.assert_clean_failure()

    def test_exact_partition_boundary_is_accepted(self):
        first = self.build()
        with mock.patch.object(font, "FONT_PARTITION_BYTES", first["output_bytes"]):
            record = self.build()
        self.assertEqual(record["partition_spare_bytes"], 0)

    def test_invalid_ui_removes_stale_artifacts(self):
        self.build()
        self.ui.write_text('draw("unterminated);', encoding="utf-8")
        with self.assertRaises(font.FontBuildError):
            self.build()
        self.assert_clean_failure()

    def test_interrupted_validation_removes_stale_artifacts(self):
        self.build()
        with mock.patch.object(font, "verify_pillow", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            self.build()
        self.assert_clean_failure()

    def test_rasterizer_failure_removes_stale_artifacts(self):
        self.build()
        with mock.patch.object(font, "verify_pillow", side_effect=OSError("FreeType failed")), self.assertRaises(OSError):
            self.build()
        self.assert_clean_failure()

    def test_empty_required_outline_rejected(self):
        synthetic_ttf(self.source, self.points, empty={ord("中")})
        self.original = self.source.read_bytes()
        with self.assertRaisesRegex(font.FontBuildError, "empty required glyph U\\+4E2D"):
            self.build()
        self.assert_clean_failure()

    def test_publication_failure_removes_partial_output(self):
        replace = os.replace
        def fail_manifest(source, target):
            if Path(target) == self.manifest:
                raise OSError("manifest publish failed")
            replace(source, target)
        with mock.patch.object(font.os, "replace", side_effect=fail_manifest), self.assertRaises(OSError):
            self.build()
        self.assert_clean_failure()

    def test_corrupt_source_is_never_modified(self):
        self.source.write_bytes(b"not a font")
        self.original = self.source.read_bytes()
        with self.assertRaises(Exception):
            self.build()
        self.assert_clean_failure()

    def test_reject_woff_even_with_ttf_extension(self):
        with TTFont(self.source) as original:
            original.flavor = "woff"
            original.save(self.source)
        self.original = self.source.read_bytes()
        with self.assertRaisesRegex(font.FontBuildError, "sfnt TrueType"):
            self.build()
        self.assert_clean_failure()

    def test_notdef_mapping_does_not_count_as_coverage(self):
        with TTFont(self.source) as original:
            for table in original["cmap"].tables:
                if table.isUnicode():
                    table.cmap[ord("中")] = ".notdef"
            original.save(self.source)
        self.original = self.source.read_bytes()
        with self.assertRaisesRegex(font.FontBuildError, "U\\+4E2D"):
            self.build()
        self.assert_clean_failure()


@unittest.skipUnless(os.environ.get("MIXOS_TEST_FONT"), "set MIXOS_TEST_FONT to enable real MiSans integration")
class RealFontTests(unittest.TestCase):
    def test_real_misans_repeatable_and_complete_ui(self):
        source = Path(os.environ["MIXOS_TEST_FONT"])
        expected_hash = "1a5f4112daaa9473747c6834041646cc9b2c338cb40ab5dbb2f0161f8968ca10"
        self.assertEqual(source.stat().st_size, 8092724)
        self.assertEqual(font.sha256(source), expected_hash)
        with tempfile.TemporaryDirectory() as temp:
            output, manifest = Path(temp) / "font.ttf", Path(temp) / "font.json"
            first = font.build(source, output, manifest)
            first_data = output.read_bytes()
            first_manifest = manifest.read_bytes()
            with mock.patch("fontTools.ttLib.tables._h_e_a_d.timestampNow", return_value=4100000000):
                second = font.build(source, output, manifest)
            self.assertEqual(first_data, output.read_bytes())
            self.assertEqual(first_manifest, manifest.read_bytes())
            self.assertEqual(first["output_sha256"], second["output_sha256"])
            self.assertEqual(first["required_coverage_missing"], [])
            self.assertEqual(first["ui_coverage_missing"], [])
            self.assertEqual(first["gb2312_included"], 7444)
            self.assertLessEqual(first["output_bytes"], font.FONT_PARTITION_BYTES)
            print("Real MiSans reproducible subset:", first["output_sha256"], first["output_bytes"], "bytes")
        self.assertEqual(font.sha256(source), expected_hash)


if __name__ == "__main__":
    unittest.main()
