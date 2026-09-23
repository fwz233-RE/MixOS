#!/usr/bin/env python3
"""Build the three official Material Symbols Rounded navigation A8 resources.

This follows build_material_icon.py (sports_esports) exactly: fix all four
axes, retain hinting, convert 960 -> 1000 UPEM without changing em proportion,
and use Pillow/FreeType BASIC grayscale rasterization. The navigation em is
60px. Ink is only cropped, never redrawn, rescaled, thresholded or filtered.
The source TTF and text font partition are read-only and never regenerated.

    py -3.12 -B tools/build_material_nav_icons.py --fetch-license
    py -3.12 -B tools/build_material_nav_icons.py --check

For an entirely offline first build, use --license-source with the existing
build/icons/material-game-icon/LICENSE.apache-2.0.txt. Subsequent builds use
the navigation artifact license. Both license paths are SHA-256 validated.
Recorded environment: fonttools==4.60.1, Pillow==11.2.1, FreeType 2.13.3.
--check regenerates all outputs in memory, compares bytes, and never writes.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import urllib.request
import zlib

import fontTools
from fontTools import subset
from fontTools.ttLib import TTFont, TTLibError
from fontTools.ttLib.scaleUpem import scale_upem
from fontTools.varLib import instancer
from PIL import Image, ImageFont, __version__ as pillow_version, features

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = ROOT / "build/icons/MaterialSymbolsRounded.ttf"
DEFAULT_HEADER = ROOT / "firmware/esp32s3/main/mix_nav_icons.h"
DEFAULT_ARTIFACTS = ROOT / "build/icons/material-nav-icons"
GENERATOR = "tools/build_material_nav_icons.py"
GENERATOR_VERSION = "1.0.0"
SIZE = 60
GLYPHS = {"arrow_back": 0xE5C4, "home": 0xE88A, "settings": 0xE8B8}
FAMILY = "Material Symbols Rounded"
# Keep identical to build_material_icon.py and build_font.py:ICON_INSTANCE.
AXES = {"FILL": 0.0, "wght": 400.0, "GRAD": 0.0, "opsz": 24.0}
SOURCE_UPEM = 960
MISANS_UPEM = 1000
FIXED_SFNT_TIME = 3029529600
UPSTREAM_COMMIT = "40a7a292a79d9394157e1ea24f83d52d5e17c556"
UPSTREAM_BASE = "https://raw.githubusercontent.com/google/material-design-icons/" + UPSTREAM_COMMIT + "/"
FONT_URL = UPSTREAM_BASE + "variablefont/MaterialSymbolsRounded%5BFILL,GRAD,opsz,wght%5D.ttf"
LICENSE_URL = UPSTREAM_BASE + "LICENSE"
SOURCE_SHA256 = "f1472f172c0fc4a922be22972e4752ccc54fe795ed82564ab6f6b097782f2dbc"
LICENSE_SHA256 = "58d1e17ffe5109a7ae296caafcadfdbe6a7d176f0bc4ab01e12a689b0499d8bd"
LICENSE_FILE = "LICENSE.apache-2.0.txt"
MANIFEST_FILE = "manifest.json"


class MaterialNavIconError(ValueError):
    """The input or generated navigation resource violates its pinned contract."""


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2) + "\n").encode("ascii")


def artifact_names() -> list[str]:
    return [*(f"{name}-{SIZE}.{ext}" for name in GLYPHS for ext in ("a8", "png")),
            LICENSE_FILE, MANIFEST_FILE]


def validate_font(font: TTFont) -> dict[str, object]:
    if (font.flavor is not None or font.sfntVersion != "\x00\x01\x00\x00"
            or "glyf" not in font or "loca" not in font):
        raise MaterialNavIconError("source must be an uncompressed TrueType font with glyf outlines")
    family = font["name"].getDebugName(1)
    copyright_text = font["name"].getDebugName(0) or ""
    if family != FAMILY or "Google" not in copyright_text:
        raise MaterialNavIconError("source must identify Google Material Symbols Rounded")
    actual = {}
    for name, codepoint in GLYPHS.items():
        glyph = (font.getBestCmap() or {}).get(codepoint)
        if glyph != name or font.getGlyphID(glyph) == 0:
            raise MaterialNavIconError(f"actual cmap U+{codepoint:04X} must map to {name}, not a fallback")
        for table in font["cmap"].tables:
            if table.isUnicode() and table.format in (4, 12) and table.cmap.get(codepoint) != name:
                raise MaterialNavIconError(f"Unicode cmap subtables disagree for U+{codepoint:04X} {name}")
        actual[name] = {"codepoint": f"U+{codepoint:04X}", "actual_cmap_glyph": glyph,
                        "source_glyph_id": font.getGlyphID(glyph)}
    if font["head"].unitsPerEm != SOURCE_UPEM:
        raise MaterialNavIconError("unexpected source unitsPerEm")
    if "fvar" not in font:
        raise MaterialNavIconError("expected the original four-axis variable source")
    axes = {a.axisTag: [a.minValue, a.defaultValue, a.maxValue] for a in font["fvar"].axes}
    if set(axes) != set(AXES):
        raise MaterialNavIconError("source variable axes do not match FILL/wght/GRAD/opsz")
    for tag, value in AXES.items():
        if not axes[tag][0] <= value <= axes[tag][2]:
            raise MaterialNavIconError(f"pinned axis value outside source range: {tag}")
    return {"family": family, "copyright": copyright_text,
            "version": font["name"].getDebugName(5), "glyphs": actual,
            "variable_axes": axes, "source_units_per_em": SOURCE_UPEM}


def validate_alpha(alpha: bytes, width: int, height: int) -> None:
    if not (0 < width <= SIZE and 0 < height <= SIZE):
        raise MaterialNavIconError("ink dimensions must fit the 60px em without resizing")
    if len(alpha) != width * height:
        raise MaterialNavIconError("A8 byte length must equal width * height")
    if not any(alpha):
        raise MaterialNavIconError("FreeType produced empty ink")
    if not any(0 < value < 255 for value in alpha):
        raise MaterialNavIconError("A8 must retain grayscale antialiasing")
    if Image.frombytes("L", (width, height), alpha).getbbox() != (0, 0, width, height):
        raise MaterialNavIconError("A8 must be tightly cropped to its actual ink boundary")


def rasterize(source_data: bytes) -> tuple[dict[str, bytes], dict[str, object]]:
    if digest(source_data) != SOURCE_SHA256:
        raise MaterialNavIconError("sourcefontSHA256 differs from the pinned Google upstream font")
    with TTFont(io.BytesIO(source_data), lazy=False, recalcTimestamp=False) as font:
        source = validate_font(font)
        instancer.instantiateVariableFont(font, AXES, inplace=True, updateFontNames=False)
        if any(table in font for table in ("fvar", "gvar", "avar", "HVAR")):
            raise MaterialNavIconError("fontTools did not produce a fully static instance")
        options = subset.Options()
        options.hinting = True
        options.layout_features = []
        options.notdef_glyph = options.notdef_outline = True
        options.recalc_timestamp = False
        options.canonical_order = True
        options.glyph_names = True
        options.drop_tables += ["STAT", "DSIG"]
        worker = subset.Subsetter(options=options)
        worker.populate(unicodes=list(GLYPHS.values()))
        worker.subset(font)
        scale_upem(font, MISANS_UPEM)
        outlines = {}
        for name, codepoint in GLYPHS.items():
            if font.getBestCmap().get(codepoint) != name:
                raise MaterialNavIconError("instancing/subsetting lost the official glyph mapping")
            outline = font["glyf"][name]
            outline.recalcBounds(font["glyf"])
            outlines[name] = [outline.xMin, outline.yMin, outline.xMax, outline.yMax]
        font["head"].created = font["head"].modified = FIXED_SFNT_TIME
        memory = io.BytesIO()
        font.save(memory, reorderTables=True)
    # Only the A8 data is published; the static font subset exists only in RAM.
    face = ImageFont.truetype(io.BytesIO(memory.getvalue()), size=SIZE,
                             layout_engine=ImageFont.Layout.BASIC)
    alphas, glyphs = {}, {}
    for name, codepoint in GLYPHS.items():
        mask, offset = face.getmask2(chr(codepoint), mode="L", anchor="la")
        if not mask.size[0] or not mask.size[1]:
            raise MaterialNavIconError(f"FreeType produced an empty mask: {name}")
        image = Image.frombytes("L", mask.size, bytes(mask))
        bbox = image.getbbox()
        if bbox is None:
            raise MaterialNavIconError(f"FreeType produced empty ink: {name}")
        ink = image.crop(bbox)  # Crop only; preserve every FreeType coverage value.
        alpha = ink.tobytes()
        validate_alpha(alpha, *ink.size)
        alphas[name] = alpha
        glyphs[name] = {
            "official_glyph_name": name, **source["glyphs"][name],
            "outline_bbox_at_scaled_upem": outlines[name],
            "mask_size_px": list(mask.size), "mask_offset_left_ascender_px": list(offset),
            "ink_bbox_in_mask_px": list(bbox),
            "ink_origin_left_ascender_px": [offset[0] + bbox[0], offset[1] + bbox[1]],
            "bitmap": {"format": "A8", "order": "row-major, top-to-bottom, left-to-right, no padding",
                       "size_px": SIZE, "width": ink.width, "height": ink.height,
                       "stride_bytes": ink.width, "bytes": len(alpha), "alphaSHA256": digest(alpha),
                       "nonzero_pixels": sum(value > 0 for value in alpha),
                       "antialiased_pixels": sum(0 < value < 255 for value in alpha),
                       "distinct_alpha_levels": len(set(alpha))},
        }
    record = {
        "schema": 1, "status": "verified", "fixed_axes": AXES,
        "source": {**source, "file": "build/icons/MaterialSymbolsRounded.ttf",
                   "sourcefontSHA256": digest(source_data), "bytes": len(source_data),
                   "upstream_repository": "https://github.com/google/material-design-icons",
                   "upstream_commit": UPSTREAM_COMMIT, "upstream_font_url": FONT_URL,
                   "upstream_identity": "Same SHA-256-pinned official source as build_material_icon.py"},
        "generator": {"path": GENERATOR, "version": GENERATOR_VERSION,
                      "sha256": digest(Path(__file__).read_bytes()), "fonttools_version": fontTools.__version__},
        "rasterizer": {"engine": "Pillow FreeType", "layout_engine": "BASIC",
                       "pillow_version": pillow_version, "freetype_version": features.version_module("freetype2"),
                       "png_zlib_version": zlib.ZLIB_VERSION, "font_size_px": SIZE,
                       "scaled_units_per_em": MISANS_UPEM,
                       "scale_policy": "960 -> 1000 UPEM, same em proportion as build_font.py merge_icons and build_material_icon.py; no fit-to-ink scaling",
                       "crop_policy": "getbbox of nonzero A8 ink, crop only; no redraw, resize, threshold or filtering"},
        "glyphs": glyphs,
        "license": {"spdx": "Apache-2.0", "file": LICENSE_FILE,
                    "sha256": LICENSE_SHA256, "upstream_url": LICENSE_URL,
                    "derivation_notice": "MixOS mechanically instanced and rasterized the official glyphs; no hand-drawn or retraced artwork."},
    }
    return alphas, record


def render_header(alphas: dict[str, bytes], record: dict[str, object]) -> bytes:
    if set(alphas) != set(GLYPHS) or set(record["glyphs"]) != set(GLYPHS):
        raise MaterialNavIconError("expected all three navigation glyphs")
    source, generator, rasterizer = record["source"], record["generator"], record["rasterizer"]
    text = f"""/* Generated by {GENERATOR} v{GENERATOR_VERSION}; do not edit.
 * Source: Google {FAMILY}, {source['version']}.
 * {source['copyright']}
 * SPDX-License-Identifier: Apache-2.0
 * Upstream commit: {UPSTREAM_COMMIT}
 * Upstream font: {FONT_URL}
 * License: {LICENSE_URL}
 * Local license/manifest: build/icons/material-nav-icons/
 * Derivation: fixed-axis instancing, em-preserving UPEM conversion and
 * FreeType A8 rasterization of real outlines. No hand drawing or retracing.
 * sourcefontSHA256: {source['sourcefontSHA256']}
 * Fixed axes: FILL0/wght400/GRAD0/opsz24 (identical to sports_esports).
 * Generator SHA256: {generator['sha256']}
 * fontTools: {generator['fonttools_version']}
 * Rasterizer: Pillow {rasterizer['pillow_version']}; FreeType {rasterizer['freetype_version']}; BASIC
 * Raster em size: {SIZE}px; UPEM: {SOURCE_UPEM} -> {MISANS_UPEM} (same as MiSans).
 * Ink crop only; WIDTH/HEIGHT describe ink, not resized 60px squares.
 * A8: row-major, top-to-bottom; stride WIDTH; no padding.
 * Regenerate: py -3.12 -B tools/build_material_nav_icons.py
 * Verify: py -3.12 -B tools/build_material_nav_icons.py --check
 */
#ifndef MIX_NAV_ICONS_H
#define MIX_NAV_ICONS_H

#include <stdint.h>

#define MIX_NAV_ICON_SIZE {SIZE}
"""
    for name, codepoint in GLYPHS.items():
        glyph, alpha = record["glyphs"][name], alphas[name]
        bitmap = glyph["bitmap"]
        width, height = bitmap["width"], bitmap["height"]
        validate_alpha(alpha, width, height)
        if digest(alpha) != bitmap["alphaSHA256"]:
            raise MaterialNavIconError(f"alphaSHA256 does not match the bitmap: {name}")
        macro = "MIX_NAV_" + name.upper()
        text += f"""
/* Official glyphname: {name}; codepoint: U+{codepoint:04X}.
 * Ink bbox in FreeType mask: {glyph['ink_bbox_in_mask_px']} (right/bottom exclusive).
 * Ink origin relative to left ascender: {glyph['ink_origin_left_ascender_px']}.
 * A8 bytes: {len(alpha)}; alphaSHA256: {bitmap['alphaSHA256']}
 */
#define {macro}_WIDTH {width}
#define {macro}_HEIGHT {height}

static const uint8_t mix_nav_{name}_alpha[{macro}_WIDTH * {macro}_HEIGHT] = {{
"""
        text += "\n".join("    " + ", ".join(f"0x{value:02x}" for value in alpha[start:start + 16]) + ","
                          for start in range(0, len(alpha), 16)) + "\n};\n"
    return (text + "\n#endif /* MIX_NAV_ICONS_H */\n").encode("ascii")


def validate_paths(source: Path, header: Path, artifacts: Path, license_source: Path | None = None) -> None:
    paths = [source, header, *(artifacts / name for name in artifact_names())]
    if license_source is not None:
        paths.append(license_source)
    for i, first in enumerate(paths):
        for second in paths[i + 1:]:
            a, b = first.resolve(), second.resolve()
            if (os.path.normcase(str(a)) == os.path.normcase(str(b))
                    or a in b.parents or b in a.parents
                    or (a.exists() and b.exists() and a.samefile(b))):
                raise MaterialNavIconError(f"input/output paths must not alias: {first} / {second}")


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def build(source: Path = DEFAULT_SOURCE, header: Path = DEFAULT_HEADER,
          artifacts: Path = DEFAULT_ARTIFACTS, *, check: bool = False,
          fetch_license: bool = False, license_source: Path | None = None) -> dict[str, object]:
    if sum((check, fetch_license, license_source is not None)) > 1:
        raise MaterialNavIconError("--check is read-only/offline; choose only one license/check mode")
    validate_paths(source, header, artifacts, license_source)
    source_data = source.read_bytes()
    if digest(source_data) != SOURCE_SHA256:
        raise MaterialNavIconError("sourcefontSHA256 differs from the pinned Google upstream font")
    license_path = artifacts / LICENSE_FILE
    if fetch_license:
        with urllib.request.urlopen(LICENSE_URL, timeout=60) as response:
            license_data = response.read()
    else:
        license_input = license_source if license_source is not None else license_path
        if not license_input.is_file():
            raise MaterialNavIconError("missing upstream license; use --license-source or --fetch-license once")
        license_data = license_input.read_bytes()
    if digest(license_data) != LICENSE_SHA256:
        raise MaterialNavIconError("upstream Apache-2.0 license SHA256 mismatch")
    alphas, record = rasterize(source_data)
    header_data = render_header(alphas, record)
    payloads = [(header, header_data)]
    record["outputs"] = {"header": {"file": "firmware/esp32s3/main/mix_nav_icons.h",
                                     "sha256": digest(header_data), "bytes": len(header_data)}}
    for name in GLYPHS:
        bitmap, alpha = record["glyphs"][name]["bitmap"], alphas[name]
        png_stream = io.BytesIO()
        Image.frombytes("L", (bitmap["width"], bitmap["height"]), alpha).save(
            png_stream, format="PNG", compress_level=9)
        png_data = png_stream.getvalue()
        for ext, data in (("a8", alpha), ("png", png_data)):
            filename = f"{name}-{SIZE}.{ext}"
            payloads.append((artifacts / filename, data))
            record["outputs"][filename] = {"file": filename, "sha256": digest(data), "bytes": len(data)}
    payloads += [(license_path, license_data), (artifacts / MANIFEST_FILE, json_bytes(record))]
    if source.read_bytes() != source_data:
        raise MaterialNavIconError("source font changed during generation")
    if check:
        different = [str(path) for path, data in payloads if not path.is_file() or path.read_bytes() != data]
        if different:
            raise MaterialNavIconError("derived outputs differ or are missing: " + ", ".join(different))
    else:
        for path, data in payloads:
            atomic_write(path, data)
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE, help="read-only pinned official TTF")
    parser.add_argument("--header", type=Path, default=DEFAULT_HEADER)
    parser.add_argument("--artifacts", type=Path, default=DEFAULT_ARTIFACTS)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="offline regeneration, compare bytes, never write")
    mode.add_argument("--fetch-license", action="store_true", help="GET pinned Google upstream Apache-2.0 LICENSE")
    mode.add_argument("--license-source", type=Path, help="read a pinned upstream license copy, offline")
    args = parser.parse_args(argv)
    try:
        record = build(args.source, args.header, args.artifacts, check=args.check,
                       fetch_license=args.fetch_license, license_source=args.license_source)
    except (MaterialNavIconError, OSError, TTLibError) as exc:
        print(f"material navigation icons failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"status": "check passed" if args.check else "generated",
                      "header": str(args.header), "header_bytes": record["outputs"]["header"]["bytes"],
                      "sourcefontSHA256": record["source"]["sourcefontSHA256"],
                      "glyphs": record["glyphs"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
