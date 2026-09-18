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
    points = set(map(ord, "%XY_gjpqryA 中文")) | {0xFFFD}
    cmap = {cp: f"u{cp:04X}" for cp in sorted(points)}
    order = [".notdef", *cmap.values()]
    builder.setupGlyphOrder(order)
    builder.setupCharacterMap(cmap)
    glyphs = {}
    for name in order:
        pen = TTGlyphPen(None)
        if name != cmap[32]:
            # Deliberately exceed advance, negative bearing, and line descent.
            pen.moveTo((-100, -300));pen.lineTo((800, -300))
            pen.lineTo((800, 800));pen.lineTo((-100, 800));pen.closePath()
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
        print(output.strip())

    @unittest.skipUnless(os.environ.get("MIXOS_TEST_RENDER_FONT"), "set MIXOS_TEST_RENDER_FONT for real-font rendering")
    def test_real_font_lifecycle_and_ink_preservation(self):
        output = execute([linux(self.exe), linux(Path(os.environ["MIXOS_TEST_RENDER_FONT"]))])
        self.assertIn("ink preservation passed", output)
        print(output.strip())

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
