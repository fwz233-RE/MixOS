"""Stable display configuration regression; no local sdkconfig or hardware needed."""
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "firmware/esp32s3/main"


class DisplayProfileTests(unittest.TestCase):
    def test_stable_board_geometry_clock_and_bounce_budget(self):
        compiler = shutil.which("clang")
        if not compiler:
            self.skipTest("clang unavailable")
        # Compile the actual UTF-8 board header without ESP-IDF or sdkconfig.h.
        # These are configuration assertions, not measured display performance.
        source = '''#include "board_pins.h"
_Static_assert(LCD_PCLK_HZ == 26000000, "stable pixel clock");
_Static_assert(LCD_H_RES == 1024 && LCD_V_RES == 768, "native resolution");
_Static_assert(LCD_BOUNCE_LINES == 16, "stable bounce lines");
_Static_assert(LCD_V_RES % LCD_BOUNCE_LINES == 0, "whole bounce chunks");
_Static_assert(2 * LCD_H_RES * LCD_BOUNCE_LINES * 2 == 65536, "64 KiB bounce pair");
_Static_assert(LCD_H_RES + LCD_HFP + LCD_HSYNC_W + LCD_HBP == 1144, "horizontal timing");
_Static_assert(LCD_V_RES + LCD_VFP + LCD_VSYNC_W + LCD_VBP == 803, "vertical timing");
'''
        with tempfile.TemporaryDirectory(prefix="mix-display-profile-") as folder:
            harness = Path(folder) / "stable_display.c"
            harness.write_text(source, encoding="utf-8")
            result = subprocess.run(
                [compiler, "-std=c11", "-Wall", "-Wextra", "-Werror",
                 "-finput-charset=UTF-8", "-fsyntax-only", "-I", str(MAIN), str(harness)],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_real_driver_uses_stable_rgb565_double_buffers(self):
        main = (MAIN / "main.c").read_text(encoding="utf-8")
        self.assertIn('#include "board_pins.h"', main)
        compact = re.sub(r"\s+", "", main)
        for field in (".clk_src=LCD_CLK_SRC_PLL240M",
                      ".pclk_hz=LCD_PCLK_HZ,.h_res=LCD_H_RES,.v_res=LCD_V_RES",
                      ".data_width=16,.bits_per_pixel=16,.num_fbs=2",
                      ".bounce_buffer_size_px=LCD_H_RES*LCD_BOUNCE_LINES",
                      ".flags.fb_in_psram=true",
                      ".on_frame_buf_complete=rgb_frame_complete"):
            with self.subTest(field=field):
                self.assertTrue(field in compact, f"main.c must configure {field}")

    def test_default_color_depth_is_rgb565(self):
        config = (MAIN.parent / "sdkconfig.defaults").read_text(encoding="utf-8")
        self.assertRegex(config, r"(?m)^CONFIG_LV_COLOR_DEPTH_16=y$")


if __name__ == "__main__":
    unittest.main()
