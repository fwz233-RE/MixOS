"""Capture the production C UI + production TTF renderer with real FreeType.

PowerShell, from the MixOS root:
  py -3.12 tests/render_ui_feedback_r6.py

No device, SSH, network, firmware build, simulated layout, or font reflow.
PNG encoding below only expands the captured RGB565 pixels losslessly into RGB8.
The UI source may be edited independently; rerun this tool after those edits.
"""
from __future__ import annotations

import argparse
import array
import hashlib
import json
import struct
import sys
import time
import zlib
from pathlib import Path

from _support import ROOT, host_run, posix_path, require_host_cc
from test_preview_ui import HEADERS

MAIN = ROOT / "firmware/esp32s3/main"
FT = ROOT / "firmware/esp32s3/managed_components/espressif__freetype/freetype"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def rgb565_to_png(source: Path, target: Path) -> None:
    """Encode existing pixels, without any text/layout/image drawing library."""
    width, height = 1024, 768
    raw = source.read_bytes()
    if len(raw) != width * height * 2:
        raise ValueError(f"Wrong framebuffer length: {source}")
    pixels = array.array("H", raw)
    if sys.byteorder != "little":
        pixels.byteswap()
    scanlines = bytearray()
    for y in range(height):
        scanlines.append(0)  # PNG filter: none
        for p in pixels[y * width:(y + 1) * width]:
            r, g, b = p >> 11, (p >> 5) & 63, p & 31
            scanlines.extend(((r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2)))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    target.write_bytes(b"\x89PNG\r\n\x1a\n" + png_chunk(b"IHDR", header)
                       + png_chunk(b"IDAT", zlib.compress(scanlines, 6)) + png_chunk(b"IEND", b""))


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--font", type=Path, default=ROOT / "build/font/MiSans-Normal-gb2312.ttf")
    parser.add_argument("--out", type=Path, default=ROOT / "build/ui-feedback-r6-preview")
    parser.add_argument("--no-sanitize", action="store_true", help="omit ASan/UBSan for a separate host timing run")
    parser.add_argument("--skip-images", action="store_true", help="run all pixel regressions without PNG encoding")
    parser.add_argument("--strict-layout", action="store_true", help="fail on ink bounds, overlap candidates, or missing glyphs")
    args = parser.parse_args()
    font, out = args.font.resolve(), args.out.resolve()
    library = ROOT / "build/host-freetype/libfreetyped.a"
    if not font.is_file() or not library.is_file():
        parser.error("Need the existing generated TTF and build/host-freetype/libfreetyped.a; see docs/ESP_FONT_BUILD.md")
    cc = require_host_cc()
    out.mkdir(parents=True, exist_ok=True)
    stub_dir = out / "host-stubs"
    # Reuse the existing UI SDK declarations but take allocation/partition/error
    # definitions from font_stubs so both production translation units agree.
    for name, content in HEADERS.items():
        if name in {"esp_err.h", "esp_heap_caps.h"}:
            continue
        dest = stub_dir / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
    inputs = [MAIN / "mix_ui.c", MAIN / "ttf_font.c", MAIN / "ttf_font.h", MAIN / "mix_terminal.c", MAIN / "mix_present.h",
              MAIN / "mix_md3.h", MAIN / "mix_game_icon.h", MAIN / "mix_nav_icons.h",
              ROOT / "tests/ui_feedback_real_harness.c", ROOT / "tests/render_ui_feedback_r6.py",
              ROOT / "tests/test_preview_ui.py", font, library]
    before = {str(p): digest(p) for p in inputs}
    exe = out / "ui_feedback_real_harness"
    command = [cc, "-std=c11", "-O2", "-g", "-Wall", "-Wextra", "-Werror", "-DESP_ERR_INVALID_ARG=5",
               "-I" + posix_path(ROOT / "tests/font_stubs"), "-I" + posix_path(stub_dir),
               "-I" + posix_path(MAIN), "-I" + posix_path(FT / "include"),
               posix_path(ROOT / "tests/ui_feedback_real_harness.c"), posix_path(MAIN / "ttf_font.c"),
               posix_path(MAIN / "mix_terminal.c"), posix_path(library),
               "-Wl,--wrap=FT_Load_Char", "-Wl,--wrap=FT_Set_Pixel_Sizes", "-lm", "-o", posix_path(exe)]
    if not args.no_sanitize:
        command[1:1] = ["-fsanitize=address,undefined", "-fno-omit-frame-pointer", "-no-pie"]
    print("编译生产 C 界面与真实 FreeType；仅生成主机可执行文件。", flush=True)
    host_run(command)
    start = time.perf_counter()
    try:
        output = host_run([posix_path(exe), posix_path(font), posix_path(out)])
    except AssertionError as error:
        (out / "run.log").write_text(str(error), encoding="utf-8")
        raise
    process_seconds = time.perf_counter() - start
    (out / "run.log").write_text(output, encoding="utf-8")
    print(output, end="")
    scenes = [json.loads(line) for line in (out / "scenes.jsonl").read_text(encoding="utf-8").splitlines()]
    scroll = next(scene for scene in scenes if scene["scene"] == "continuous-touch-scroll")
    assert "CONTINUOUS_TOUCH_SCROLL_OK" in output
    assert scroll["byte_equal"] and scroll["panel_equal"]
    assert scroll["sections"] == 5 and scroll["languages"] == 2 and scroll["themes"] == 12
    assert scroll["roundtrips"] == 5 * 2 * 12 * 2
    assert all(scroll[key] > 0 for key in ("partial_frames", "one_pixel_frames", "large_delta_frames",
                                          "invalidation_checks", "clipped_text_calls"))
    images = []
    for scene in scenes:
        source = out / (scene["scene"] + ".rgb565")
        # Only image-bearing scenes are encoded, avoiding stale files from an
        # earlier version of this capture suite.
        if not scene["image"] or args.skip_images:
            continue
        if not source.is_file():
            raise AssertionError(f"Missing raw capture for {scene['scene']}")
        target = source.with_suffix(".png")
        rgb565_to_png(source, target)
        images.append({"scene": scene["scene"], "png": target.name,
                       "rgb565_sha256": digest(source), "png_sha256": digest(target)})
    changed = [str(p) for p in inputs if before[str(p)] != digest(p)]
    issues = sum(scene.get("layout_issue_count", 0) for scene in scenes)
    missing = sorted({cp for scene in scenes for cp in scene.get("missing_codepoints", [])})
    summary = {
        "renderer": "unchanged production mix_ui.c + ttf_font.c + vendored FreeType static library",
        "pixel_format": "1024x768 little-endian RGB565, expanded to RGB8 PNG without reflow",
        "host_only": True, "physical_display_tested": False,
        "timing_scope": "host CPU wall time with a no-wait audited presenter; not device FPS or LCD timing",
        "sanitizers": not args.no_sanitize, "host_process_seconds": process_seconds,
        "stubbed_inputs": "illustrative telemetry/network/history with explicit invalidation changes, NVS, no-wait audited presenter and virtual clock",
        "ui_framebuffer_count": 1, "ui_framebuffer_bytes": 1572864,
        "test_oracle_bytes": scroll["test_oracle_bytes"],
        "scroll_regression": scroll,
        "source_hashes": before, "inputs_changed_during_run": changed,
        "layout_issues": issues, "missing_codepoints": [f"U+{cp:04X}" for cp in missing],
        "images": images, "scenes": scenes,
        "checks": ["real glyph alpha bounds and pairwise ink-AABB overlap candidates",
                   "visible glyph pixels with alpha>=250 retain their exact foreground in the final framebuffer",
                   "UTF-8 code-point parsing and production advances agree",
                   "all five settings pages retain rail/footer pixels on body-only scrolling",
                   "real touch drags in both directions: 5 sections x 2 languages x 12 themes x 2 roundtrips",
                   "every rendered touch frame byte-equals a fresh production full redraw, including scrollbar",
                   "submitted panel pixels equal the reference, catching strip-only or missing body presents",
                   "actual aligned, non-overlapping row memcpy calls prove scroll reuse, including one-pixel deltas",
                   "coalesced legal touch samples exercise greater-than-viewport full-redraw fallback",
                   "draw_clip_y0, draw_limit_y and draw_offset_y recover after every frame",
                   "network, power/history, notice and toast changes invalidate reused pixels during pending drags",
                   "no-wait batch presenter audits 1..8 rectangle bounds and counts one present per batch",
                   "20 warm full renders per settings page perform zero FT loads/size changes",
                   "fully offscreen text/fit/center/icon primitives perform zero font calls",
                   "single guarded production UI framebuffer", "lock returns to exact idle pixels"],
    }
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"生成 {len(images)} 张生产像素 PNG：{out}")
    print(f"布局候选问题 {issues}；字体缺失码点 {summary['missing_codepoints']}；主机进程耗时 {process_seconds:.3f}s。")
    if changed:
        print("运行期间生产输入发生变化，请在修改完成后重新运行：", *changed, sep="\n")
        return 2
    if args.strict_layout and (issues or missing):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
