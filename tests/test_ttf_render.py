"""Native production renderer regressions (WSL on Windows, local cc on Linux).

Build the vendored FreeType using the commands in docs/ESP_FONT_BUILD.md
before running. The synthetic test font needs fontTools; real MiSans
integration is opt-in through MIXOS_TEST_RENDER_FONT and never needs a board.
"""
import os
import unittest
from pathlib import Path

from _support import ROOT, posix_path, host_run, require_host_cc

try:
    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.ttGlyphPen import TTGlyphPen
except ImportError:
    FontBuilder = None

OUT = ROOT / "build/host-font-render"
FT = ROOT / "firmware/esp32s3/managed_components/espressif__freetype/freetype"


def linux(path):
    return posix_path(path)


def execute(args):
    return host_run(args)


def synthetic_font(path):
    builder = FontBuilder(1000, isTTF=True)
    points = set(map(ord, "%XY_gjpqryAWMi éüǜ中文国圆田回")) | {0xFFFD}
    cmap = {cp: f"u{cp:04X}" for cp in sorted(points)}
    order = [".notdef", *cmap.values()]
    builder.setupGlyphOrder(order)
    builder.setupCharacterMap(cmap)
    glyphs = {}
    for name in order:
        pen = TTGlyphPen(None)
        if name != cmap[32]:
            # Deliberately exceed advance, negative bearing, and line descent.
            bottom = -450 if name == cmap[0x01DC] else -300
            pen.moveTo((-100, bottom));pen.lineTo((800, bottom))
            pen.lineTo((800, 800));pen.lineTo((-100, 800));pen.closePath()
            if name in (cmap[0x00E9], cmap[0x00FC], cmap[0x01DC]):
                # A detached accent above the declared ascender must survive
                # the SAME scale as the body and any exceptional descender.
                pen.moveTo((250, 1050));pen.lineTo((350, 1050))
                pen.lineTo((350, 1150));pen.lineTo((250, 1150));pen.closePath()
        glyphs[name] = pen.glyph()
    builder.setupGlyf(glyphs)
    builder.setupHorizontalMetrics({name: (600, -100) for name in order})
    builder.setupHorizontalHeader(ascent=800, descent=-200)
    builder.setupNameTable({"familyName": "MixOS Renderer Test", "styleName": "Regular",
                           "uniqueFontIdentifier": "MixOS-Renderer-1", "fullName": "MixOS Renderer Test",
                           "psName": "MixOS-Renderer-Test", "version": "Version 1.0"})
    builder.setupOS2(sTypoAscender=800, sTypoDescender=-200, usWinAscent=800, usWinDescent=200)
    builder.setupPost();builder.setupMaxp();builder.save(path)


class TtfRendererTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if FontBuilder is None:
            raise unittest.SkipTest("fontTools needed for generated regression fixture")
        cc = require_host_cc()
        library = ROOT / "build/host-freetype/libfreetyped.a"
        if not library.exists():
            raise unittest.SkipTest(
                "build the vendored FreeType static library first "
                "(see docs/TESTING.md, 'Host FreeType')")
        OUT.mkdir(parents=True, exist_ok=True)
        cls.exe = OUT / "ttf_render_harness"
        cls.synthetic = OUT / "synthetic.ttf"
        synthetic_font(cls.synthetic)
        execute([cc, "-std=c11", "-Wall", "-Wextra", "-Werror", "-g", "-O1",
                 "-fsanitize=address,undefined", "-fno-omit-frame-pointer", "-no-pie",
                 "-I" + linux(ROOT / "tests/font_stubs"), "-I" + linux(FT / "include"),
                 linux(ROOT / "tests/ttf_render_harness.c"), linux(library), "-lm", "-o", linux(cls.exe)])

    def test_synthetic_metrics_lifecycle_and_cell_bounds(self):
        output = execute([linux(self.exe), linux(self.synthetic)])
        self.assertIn("ink preservation passed", output)
        self.assertIn("Terminal contrast passed: 256 coverage levels", output)
        self.assertIn("Notes 34px cells passed", output)
        print(output.strip())

    @unittest.skipUnless(os.environ.get("MIXOS_TEST_RENDER_FONT"), "set MIXOS_TEST_RENDER_FONT for real-font rendering")
    def test_real_font_lifecycle_and_ink_preservation(self):
        output = execute([linux(self.exe), linux(Path(os.environ["MIXOS_TEST_RENDER_FONT"]))])
        self.assertIn("ink preservation passed", output)
        self.assertIn("Terminal contrast passed: 256 coverage levels", output)
        self.assertIn("Notes 34px cells passed", output)
        print(output.strip())

    def check_cell_proportions(self, font):
        output = execute([linux(self.exe), linux(font), "--cell-proportions"])
        self.assertIn("Cell proportions passed: sizes=20,26,34 CJK=2 Latin=1", output)
        self.assertIn("accents bold clipping uniform_reference=1", output)
        self.assertIn("cases=798 mismatches=0", output)
        self.assertIn("sizes=26,34 accents descenders baseline bold guards", output)
        self.assertIn("CELL extreme bounds passed: 1..64px cells, 1..255px fonts", output)
        print(output.strip())

    def test_synthetic_uniform_cell_proportions(self):
        self.check_cell_proportions(self.synthetic)

    @unittest.skipUnless(os.environ.get("MIXOS_TEST_RENDER_FONT"), "set MIXOS_TEST_RENDER_FONT for real-font rendering")
    def test_real_font_uniform_cell_proportions(self):
        self.check_cell_proportions(Path(os.environ["MIXOS_TEST_RENDER_FONT"]))

    def check_clipped_text(self, font):
        output = execute([linux(self.exe), linux(font), "--clipped-text"])
        self.assertIn("TTF clipped text passed:", output)
        self.assertIn("outside_unchanged=1 inside_full_equal=1", output)
        self.assertIn("sizes=13 invalid_clips=8", output)
        print(output.strip())

    def test_synthetic_clipped_text_bounds_and_full_render_equivalence(self):
        self.check_clipped_text(self.synthetic)

    @unittest.skipUnless(os.environ.get("MIXOS_TEST_RENDER_FONT"), "set MIXOS_TEST_RENDER_FONT for real-font rendering")
    def test_real_font_clipped_text_bounds_and_full_render_equivalence(self):
        self.check_clipped_text(Path(os.environ["MIXOS_TEST_RENDER_FONT"]))

    def check_cache(self, font):
        output = execute([linux(self.exe), linux(font), "--cache-benchmark"])
        self.assertIn("TTF cache regressions passed", output)
        self.assertIn("phase=warm size_calls=0 loads=0", output)
        self.assertIn("hot_reloads=0/32", output)
        self.assertIn("retained=1984", output)
        self.assertIn("budget=524288", output)
        print(output.strip())

    def test_synthetic_cache_reuse_pressure_and_lifetime(self):
        self.check_cache(self.synthetic)

    @unittest.skipUnless(os.environ.get("MIXOS_TEST_RENDER_FONT"), "set MIXOS_TEST_RENDER_FONT for real-font rendering")
    def test_real_font_cache_reuse_pressure_and_lifetime(self):
        self.check_cache(Path(os.environ["MIXOS_TEST_RENDER_FONT"]))

    def test_startup_initializes_font_before_ui(self):
        main = (ROOT / "firmware/esp32s3/main/main.c").read_text(encoding="utf-8")
        startup = main[main.index("void app_main(void)"):]
        self.assertLess(startup.index("ttf_font_init()"), startup.index("mix_ui_init(panel)"))
        # A missing font now follows the bounded trial-reset / VALID USB
        # maintenance policy; merely logging and continuing could confirm a
        # candidate whose required local UI never initialized.
        self.assertIn('if((e=ttf_font_init())!=ESP_OK){startup_failure("font initialization",e);maintenance_loop();}', startup)
        ui = (ROOT / "firmware/esp32s3/main/mix_ui.c").read_text(encoding="utf-8")
        self.assertIn("ttf_draw_cell(fb,W,H,x,y,w,h,size,fg,cp,bold)", ui)
        self.assertNotIn("scratch[32*24]", ui)


if __name__ == "__main__":
    unittest.main()
