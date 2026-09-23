#!/usr/bin/env python3
"""Build the official Material Symbols Rounded sports_esports A8 resource.

The input is read-only. No MiSans font, UI source, compiler or device is used.
70 px is the em size (ICON28dp), NOT a request to enlarge the ink to 70 px.
Only fontTools axis instancing / UPEM conversion and FreeType rasterization
are performed; the outline and the rasterized pixels are never redrawn,
resized, dilated, thresholded or otherwise visually adjusted.

    py -3.12 -B tools/build_material_icon.py --fetch-license
    py -3.12 -B tools/build_material_icon.py --check

Install fonttools==4.60.1 and Pillow==11.2.1 for the recorded generation
(FreeType 2.13.3). --check is offline and byte-compares every derived output;
it reports differences rather than refreshing files or accepting stale data.
The optional download reads only the pinned Google repository LICENSE.
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
DEFAULT_HEADER = ROOT / "firmware/esp32s3/main/mix_game_icon.h"
DEFAULT_ARTIFACTS = ROOT / "build/icons/material-game-icon"
GENERATOR = "tools/build_material_icon.py"
GENERATOR_VERSION = "1.0.0"
SIZE = 70
CODEPOINT = 0xEA28
GLYPH_NAME = "sports_esports"
FAMILY = "Material Symbols Rounded"
AXES = {"FILL": 0.0, "wght": 400.0, "GRAD": 0.0, "opsz": 24.0}
# Same coordinate conversion as tools/build_font.py:merge_icons for MiSans
# 4.003. It preserves the em proportion; there is no fit-to-ink enlargement.
SOURCE_UPEM = 960
MISANS_UPEM = 1000
FIXED_SFNT_TIME = 3029529600
# Byte-for-byte verified against this immutable Google upstream revision.
UPSTREAM_COMMIT = "40a7a292a79d9394157e1ea24f83d52d5e17c556"
UPSTREAM_BASE = "https://raw.githubusercontent.com/google/material-design-icons/" + UPSTREAM_COMMIT + "/"
FONT_URL = UPSTREAM_BASE + "variablefont/MaterialSymbolsRounded%5BFILL,GRAD,opsz,wght%5D.ttf"
LICENSE_URL = UPSTREAM_BASE + "LICENSE"
SOURCE_SHA256 = "f1472f172c0fc4a922be22972e4752ccc54fe795ed82564ab6f6b097782f2dbc"
LICENSE_SHA256 = "58d1e17ffe5109a7ae296caafcadfdbe6a7d176f0bc4ab01e12a689b0499d8bd"
LICENSE_FILE = "LICENSE.apache-2.0.txt"
ALPHA_FILE = "sports_esports-70.a8"
PNG_FILE = "sports_esports-70.png"
MANIFEST_FILE = "manifest.json"


class MaterialIconError(ValueError):
    """The source or generated resource does not match the reviewed contract."""


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2) + "\n").encode("ascii")


def validate_font(font: TTFont) -> dict[str, object]:
    """Validate actual cmap and font identity, never infer a glyph from its PUA."""
    if (font.flavor is not None or font.sfntVersion != "\x00\x01\x00\x00"
            or "glyf" not in font or "loca" not in font):
        raise MaterialIconError("source must be an uncompressed TrueType font with glyf outlines")
    family = font["name"].getDebugName(1)
    copyright_text = font["name"].getDebugName(0) or ""
    if family != FAMILY or "Google" not in copyright_text:
        raise MaterialIconError("source must identify Google Material Symbols Rounded")
    glyph = (font.getBestCmap() or {}).get(CODEPOINT)
    if glyph != GLYPH_NAME or font.getGlyphID(glyph) == 0:
        raise MaterialIconError("actual cmap U+EA28 must map to sports_esports, not a fallback")
    for table in font["cmap"].tables:
        if table.isUnicode() and table.format in (4, 12):
            if table.cmap.get(CODEPOINT) != GLYPH_NAME:
                raise MaterialIconError("Unicode cmap subtables disagree for U+EA28 sports_esports")
    if font["head"].unitsPerEm != SOURCE_UPEM:
        raise MaterialIconError("unexpected source unitsPerEm")
    if "fvar" not in font:
        raise MaterialIconError("expected the original four-axis variable source")
    axes = {a.axisTag: [a.minValue, a.defaultValue, a.maxValue] for a in font["fvar"].axes}
    if set(axes) != set(AXES):
        raise MaterialIconError("source variable axes do not match FILL/wght/GRAD/opsz")
    for tag, value in AXES.items():
        if not axes[tag][0] <= value <= axes[tag][2]:
            raise MaterialIconError(f"pinned axis value outside source range: {tag}")
    return {"family": family, "copyright": copyright_text,
            "version": font["name"].getDebugName(5), "actual_cmap_glyph": glyph,
            "source_glyph_id": font.getGlyphID(glyph), "variable_axes": axes,
            "source_units_per_em": SOURCE_UPEM}


def validate_alpha(alpha: bytes, width: int, height: int) -> None:
    if not (0 < width <= SIZE and 0 < height <= SIZE):
        raise MaterialIconError("ink dimensions must fit the 70px em without resizing")
    if len(alpha) != width * height:
        raise MaterialIconError("A8 byte length must equal width * height")
    if not any(alpha):
        raise MaterialIconError("FreeType produced empty ink")
    if not any(0 < value < 255 for value in alpha):
        raise MaterialIconError("A8 must retain grayscale antialiasing")
    if Image.frombytes("L", (width, height), alpha).getbbox() != (0, 0, width, height):
        raise MaterialIconError("A8 must be tightly cropped to its actual ink boundary")


def rasterize(source_data: bytes) -> tuple[bytes, dict[str, object]]:
    if digest(source_data) != SOURCE_SHA256:
        raise MaterialIconError("sourcefontSHA256 differs from the pinned Google upstream font")
    with TTFont(io.BytesIO(source_data), lazy=False, recalcTimestamp=False) as font:
        source = validate_font(font)
        # Same instancing then subsetting order and policy as merge_icons.
        instancer.instantiateVariableFont(font, AXES, inplace=True, updateFontNames=False)
        if any(table in font for table in ("fvar", "gvar", "avar", "HVAR")):
            raise MaterialIconError("fontTools did not produce a fully static instance")
        options = subset.Options()
        options.hinting = True
        options.layout_features = []
        options.notdef_glyph = options.notdef_outline = True
        options.recalc_timestamp = False
        options.canonical_order = True
        options.glyph_names = True
        options.drop_tables += ["STAT", "DSIG"]
        worker = subset.Subsetter(options=options)
        worker.populate(unicodes=[CODEPOINT])
        worker.subset(font)
        scale_upem(font, MISANS_UPEM)
        if font.getBestCmap().get(CODEPOINT) != GLYPH_NAME:
            raise MaterialIconError("instancing/subsetting lost the official glyph mapping")
        outline = font["glyf"][GLYPH_NAME]
        outline.recalcBounds(font["glyf"])
        outline_bbox = [outline.xMin, outline.yMin, outline.xMax, outline.yMax]
        font["head"].created = font["head"].modified = FIXED_SFNT_TIME
        memory = io.BytesIO()
        font.save(memory, reorderTables=True)
    # The static subset exists only in RAM, never in the text font partition.
    face = ImageFont.truetype(io.BytesIO(memory.getvalue()), size=SIZE,
                             layout_engine=ImageFont.Layout.BASIC)
    mask, offset = face.getmask2(chr(CODEPOINT), mode="L", anchor="la")
    if not mask.size[0] or not mask.size[1]:
        raise MaterialIconError("FreeType produced an empty mask")
    image = Image.frombytes("L", mask.size, bytes(mask))
    ink_bbox = image.getbbox()
    if ink_bbox is None:
        raise MaterialIconError("FreeType produced empty ink")
    ink = image.crop(ink_bbox)  # Pure crop: no resampling or modification of coverage.
    alpha = ink.tobytes()
    validate_alpha(alpha, *ink.size)
    record = {
        "schema": 1, "status": "verified", "official_glyph_name": GLYPH_NAME,
        "codepoint": f"U+{CODEPOINT:04X}", "fixed_axes": AXES,
        "source": {**source, "file": "build/icons/MaterialSymbolsRounded.ttf",
                   "sourcefontSHA256": digest(source_data), "bytes": len(source_data),
                   "upstream_repository": "https://github.com/google/material-design-icons",
                   "upstream_commit": UPSTREAM_COMMIT, "upstream_font_url": FONT_URL,
                   "upstream_identity": "SHA-256 pinned after byte-for-byte comparison with the official font"},
        "generator": {"path": GENERATOR, "version": GENERATOR_VERSION,
                      "sha256": digest(Path(__file__).read_bytes()),
                      "fonttools_version": fontTools.__version__},
        "rasterizer": {"engine": "Pillow FreeType", "layout_engine": "BASIC",
                       "pillow_version": pillow_version,
                       "freetype_version": features.version_module("freetype2"),
                       "png_zlib_version": zlib.ZLIB_VERSION,
                       "font_size_px": SIZE, "scaled_units_per_em": MISANS_UPEM,
                       "scale_policy": "960 -> 1000 UPEM, same em proportion as build_font.py merge_icons; no fit-to-ink scaling",
                       "outline_bbox_at_scaled_upem": outline_bbox,
                       "mask_size_px": list(mask.size), "mask_offset_left_ascender_px": list(offset),
                       "ink_bbox_in_mask_px": list(ink_bbox),
                       "ink_origin_left_ascender_px": [offset[0] + ink_bbox[0], offset[1] + ink_bbox[1]],
                       "crop_policy": "getbbox of nonzero A8 ink, crop only; no redraw, resize, threshold or filtering"},
        "bitmap": {"format": "A8", "order": "row-major, top-to-bottom, left-to-right, no padding",
                   "size_px": SIZE, "width": ink.width, "height": ink.height,
                   "stride_bytes": ink.width, "bytes": len(alpha), "alphaSHA256": digest(alpha),
                   "nonzero_pixels": sum(value > 0 for value in alpha),
                   "antialiased_pixels": sum(0 < value < 255 for value in alpha),
                   "distinct_alpha_levels": len(set(alpha))},
        "license": {"spdx": "Apache-2.0", "file": LICENSE_FILE,
                    "sha256": LICENSE_SHA256, "upstream_url": LICENSE_URL,
                    "derivation_notice": "MixOS mechanically instanced and rasterized the official glyph; no hand-drawn or retraced artwork."},
    }
    return alpha, record


def render_header(alpha: bytes, record: dict[str, object]) -> bytes:
    bitmap, source = record["bitmap"], record["source"]
    generator, rasterizer = record["generator"], record["rasterizer"]
    width, height = bitmap["width"], bitmap["height"]
    validate_alpha(alpha, width, height)
    if digest(alpha) != bitmap["alphaSHA256"]:
        raise MaterialIconError("alphaSHA256 does not match the bitmap")
    rows = ["    " + ", ".join(f"0x{value:02x}" for value in alpha[start:start + 16]) + ","
            for start in range(0, len(alpha), 16)]
    text = f"""/* Generated by {GENERATOR} v{GENERATOR_VERSION}; do not edit.
 * Official glyphname: {GLYPH_NAME}; codepoint: U+EA28.
 * Source: Google {FAMILY}, {source['version']}.
 * {source['copyright']}
 * SPDX-License-Identifier: Apache-2.0
 * Upstream commit: {UPSTREAM_COMMIT}
 * Upstream font: {FONT_URL}
 * License: {LICENSE_URL}
 * Local license/manifest: build/icons/material-game-icon/
 * Derivation: fixed-axis instancing, em-preserving UPEM conversion and
 * FreeType A8 rasterization of the real outline. No hand drawing or retracing.
 * sourcefontSHA256: {source['sourcefontSHA256']}
 * Fixed axes: FILL0/wght400/GRAD0/opsz24
 * Generator SHA256: {generator['sha256']}
 * fontTools: {generator['fonttools_version']}
 * Rasterizer: Pillow {rasterizer['pillow_version']}; FreeType {rasterizer['freetype_version']}; BASIC
 * Raster em size: {SIZE}px; UPEM: {SOURCE_UPEM} -> {MISANS_UPEM} (same as MiSans).
 * Ink crop only; WIDTH/HEIGHT describe ink, not a resized 70px square.
 * A8: row-major, top-to-bottom; stride WIDTH; {len(alpha)} bytes.
 * alphaSHA256: {bitmap['alphaSHA256']}
 * Regenerate: py -3.12 -B tools/build_material_icon.py
 * Verify: py -3.12 -B tools/build_material_icon.py --check
 */
#ifndef MIX_GAME_ICON_H
#define MIX_GAME_ICON_H

#include <stdint.h>

#define MIX_GAME_ICON_SIZE {SIZE}
#define MIX_GAME_ICON_WIDTH {width}
#define MIX_GAME_ICON_HEIGHT {height}

static const uint8_t mix_game_icon_alpha[MIX_GAME_ICON_WIDTH * MIX_GAME_ICON_HEIGHT] = {{
""" + "\n".join(rows) + "\n};\n\n#endif /* MIX_GAME_ICON_H */\n"
    return text.encode("ascii")


def validate_paths(source: Path, header: Path, artifacts: Path) -> None:
    # Prevent even hard-link aliases from writing back to the read-only source.
    paths = [source, header, *(artifacts / name for name in
                              (ALPHA_FILE, PNG_FILE, MANIFEST_FILE, LICENSE_FILE))]
    for i, first in enumerate(paths):
        for second in paths[i + 1:]:
            a, b = first.resolve(), second.resolve()
            if (os.path.normcase(str(a)) == os.path.normcase(str(b))
                    or a in b.parents or b in a.parents
                    or (a.exists() and b.exists() and a.samefile(b))):
                raise MaterialIconError(f"input/output paths must not alias: {first} / {second}")


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
          fetch_license: bool = False) -> dict[str, object]:
    if check and fetch_license:
        raise MaterialIconError("--check is read-only/offline; use --fetch-license separately")
    validate_paths(source, header, artifacts)
    # Fail cheaply on invalid inputs before starting any optional network read.
    source_data = source.read_bytes()
    if digest(source_data) != SOURCE_SHA256:
        raise MaterialIconError("sourcefontSHA256 differs from the pinned Google upstream font")
    license_path = artifacts / LICENSE_FILE
    if fetch_license:
        with urllib.request.urlopen(LICENSE_URL, timeout=60) as response:
            license_data = response.read()
    else:
        if not license_path.is_file():
            raise MaterialIconError("missing upstream license; run once with --fetch-license")
        license_data = license_path.read_bytes()
    if digest(license_data) != LICENSE_SHA256:
        raise MaterialIconError("upstream Apache-2.0 license SHA256 mismatch")
    alpha, record = rasterize(source_data)
    header_data = render_header(alpha, record)
    bitmap = record["bitmap"]
    png_stream = io.BytesIO()
    Image.frombytes("L", (bitmap["width"], bitmap["height"]), alpha).save(
        png_stream, format="PNG", compress_level=9)
    png_data = png_stream.getvalue()
    record["outputs"] = {
        "header": {"file": "firmware/esp32s3/main/mix_game_icon.h",
                   "sha256": digest(header_data), "bytes": len(header_data)},
        "alpha": {"file": ALPHA_FILE, "sha256": digest(alpha), "bytes": len(alpha)},
        "png": {"file": PNG_FILE, "sha256": digest(png_data), "bytes": len(png_data)},
    }
    payloads = [(header, header_data), (artifacts / ALPHA_FILE, alpha),
                (artifacts / PNG_FILE, png_data), (license_path, license_data),
                (artifacts / MANIFEST_FILE, json_bytes(record))]
    if source.read_bytes() != source_data:
        raise MaterialIconError("source font changed during generation")
    if check:
        different = [str(path) for path, data in payloads
                     if not path.is_file() or path.read_bytes() != data]
        if different:
            raise MaterialIconError("derived outputs differ or are missing: " + ", ".join(different))
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
    args = parser.parse_args(argv)
    try:
        record = build(args.source, args.header, args.artifacts, check=args.check,
                       fetch_license=args.fetch_license)
    except (MaterialIconError, OSError, TTLibError) as exc:
        print(f"material icon failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"status": "check passed" if args.check else "generated",
                      "header": str(args.header), "header_bytes": record["outputs"]["header"]["bytes"],
                      "sourcefontSHA256": record["source"]["sourcefontSHA256"],
                      **record["bitmap"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
