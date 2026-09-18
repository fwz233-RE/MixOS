#!/usr/bin/env python3
"""Build a verified, reproducible raw sfnt/TrueType image for MixOS.

All printable C string literals in mix_ui.c are a conservative UI requirement
superset (both languages, all branches, arrays and format strings). No compiler
or target hardware is needed. See docs/FONT_BUILD.md for scope and limitations.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import unicodedata

import fontTools
from fontTools import subset
from fontTools.ttLib import TTFont, TTLibError
from fontTools.ttLib.scaleUpem import scale_upem
from fontTools.varLib import instancer
from PIL import ImageFont, __version__ as pillow_version, features

FONT_PARTITION_BYTES = 0x400000
FONT_PARTITION_OFFSET = "0x210000"
# Seconds since 1904-01-01, corresponding to 2000-01-01T00:00:00Z.
FIXED_SFNT_TIME = 3029529600
DEFAULT_UI = Path(__file__).resolve().parents[1] / "firmware/esp32s3/main/mix_ui.c"
# Interface icons come from a second face. MiSans maps nothing in the Private
# Use Area, so icon glyphs are purely additive and cannot displace a text
# glyph; a UI codepoint in this range is looked up in the icon font instead of
# being demanded from MiSans. Keep in sync with mix_ui.c's ICON_* literals.
DEFAULT_ICONS = Path(__file__).resolve().parents[1] / "build/icons/MaterialSymbolsRounded.ttf"
ICON_FIRST, ICON_LAST = 0xE000, 0xF8FF
# The icon source is a four-axis variable font. FreeType would rasterize its
# default instance, but shipping the variation tables to a device that can
# never move an axis only costs flash, so it is pinned to one static instance
# here. opsz 24 is the design size for the 20-24 px status bar glyphs.
ICON_INSTANCE = {"FILL": 0.0, "wght": 400.0, "GRAD": 0.0, "opsz": 24.0}
BASIC_LATIN = set(range(0x20, 0x7F))
LATIN_1 = set(range(0xA0, 0x100))
EXTRA_TEXT = "€℃·…—–“”‘’→←↑↓±✓✕△◇□○"
RUNTIME_REQUIRED = {0xFFFD}  # mix_terminal.c emits this for invalid UTF-8.
UI_SIZES = (14, 17, 18, 20, 22, 23, 24, 26, 30, 32, 64)
# Explicit, reviewed visual fallback only; never silently drop a requirement.
GLYPH_ALIASES: dict[int, tuple[int, str]] = {
    0xFFFD: (0x003F, "ASCII question mark visibly marks invalid UTF-8; original source has no replacement-character glyph. Not the standard diamond outline."),
}
C_TOKEN = re.compile(
    r'(?P<comment>/\*.*?\*/|//[^\n]*)|'
    r'(?P<string>(?:u8|[uUL])?"(?:[^"\\\n]|\\[^\n])*")|'
    r"(?P<char>(?:[uUL])?'(?:[^'\\\n]|\\[^\n])*')|(?P<other>.)",
    re.DOTALL,
)
ESCAPE = {"a": 7, "b": 8, "f": 12, "n": 10, "r": 13, "t": 9, "v": 11,
          "\\": 92, "'": 39, '"': 34, "?": 63}


class FontBuildError(ValueError):
    """The requested image cannot be safely certified."""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def gb2312_codepoints() -> set[int]:
    result: set[int] = set()
    for lead in range(0xA1, 0xF8):
        for trail in range(0xA1, 0xFF):
            try:
                result.update(map(ord, bytes((lead, trail)).decode("gb2312")))
            except UnicodeDecodeError:
                pass
    return result


def unicode_cmap(font: TTFont) -> set[int]:
    """Use the preferred cmap, not a misleading union of incompatible subtables."""
    return {cp for cp, name in (font.getBestCmap() or {}).items()
            if name != ".notdef" and font.getGlyphID(name) != 0}


def codepoint_records(points: set[int]) -> list[dict[str, object]]:
    return [{"codepoint": f"U+{cp:04X}", "character": chr(cp),
             "name": unicodedata.name(chr(cp), "UNNAMED")} for cp in sorted(points)]


def decode_c_string(token: str) -> str:
    prefix = token[:token.index('"')]
    if prefix not in ("", "u8"):
        raise FontBuildError("wide C string literals require an explicit extraction policy")
    body = token[token.index('"') + 1:-1]
    data = bytearray()
    i = 0
    while i < len(body):
        ch = body[i]
        i += 1
        if ch != "\\":
            data.extend(ch.encode("utf-8"))
            continue
        ch = body[i]
        i += 1
        if ch in ESCAPE:
            data.append(ESCAPE[ch])
        elif ch in "01234567":
            match = re.match(r"[0-7]{0,2}", body[i:])
            digits = ch + match.group()
            i += len(digits) - 1
            value = int(digits, 8)
            if value > 255:
                raise FontBuildError("out-of-range C byte escape")
            data.append(value)
        elif ch == "x":
            match = re.match(r"[0-9a-fA-F]+", body[i:])
            if not match or int(match.group(), 16) > 255:
                raise FontBuildError("invalid C hexadecimal byte escape")
            data.append(int(match.group(), 16))
            i += len(match.group())
        elif ch in "uU":
            n = 4 if ch == "u" else 8
            digits = body[i:i+n]
            if len(digits) != n or not re.fullmatch(r"[0-9a-fA-F]+", digits):
                raise FontBuildError("invalid C Unicode escape")
            data.extend(chr(int(digits, 16)).encode("utf-8"))
            i += n
        else:
            raise FontBuildError(f"unsupported C escape: \\{ch}")
    return data.decode("utf-8", errors="strict")


def extract_ui(source: str) -> tuple[set[int], list[dict[str, object]]]:
    # Translation phase 2: remove escaped newlines before recognizing comments.
    source = re.sub(r"\\\r?\n", "", source)
    points: set[int] = set()
    literals: list[dict[str, object]] = []
    for match in C_TOKEN.finditer(source):
        if match.lastgroup == "string":
            text = decode_c_string(match.group())
            visible = {ord(ch) for ch in text if ord(ch) >= 32 and not 0x7F <= ord(ch) < 0xA0}
            points.update(visible)
            literals.append({"line_after_splicing": source.count("\n", 0, match.start()) + 1,
                             "text": text})
        elif match.lastgroup == "other" and match.group() in ('"', "'"):
            raise FontBuildError("unterminated or unsupported C literal")
        elif match.lastgroup == "other" and source[match.start():].startswith("/*"):
            raise FontBuildError("unterminated C comment")
    if not literals or not points:
        raise FontBuildError("UI source contains no string literals")
    return points, literals


def validate_paths(source: Path, output: Path, manifest: Path, ui_source: Path,
                   icon_source: Path | None = None) -> tuple[Path, ...]:
    inputs = [source, output, manifest, ui_source]
    if icon_source is not None:
        inputs.append(icon_source)
    paths = tuple(Path(p).resolve() for p in inputs)
    for i, first in enumerate(paths):
        for second in paths[i+1:]:
            if (os.path.normcase(str(first)) == os.path.normcase(str(second))
                    or (first.exists() and second.exists() and first.samefile(second))):
                raise FontBuildError(f"paths must not alias one another: {first} / {second}")
            if first in second.parents or second in first.parents:
                raise FontBuildError(f"file paths must not contain one another: {first} / {second}")
    for target in paths[1:3]:
        if target.exists() and not target.is_file():
            raise FontBuildError(f"destination is not a regular file: {target}")
    return paths


def require_truetype(font: TTFont) -> None:
    if font.flavor is not None or font.sfntVersion != "\x00\x01\x00\x00" or "glyf" not in font or "loca" not in font:
        raise FontBuildError("expected uncompressed single-face sfnt TrueType (00010000, glyf/loca), not WOFF/CFF/TTC")
    if "fvar" in font:
        raise FontBuildError("variable fonts require an explicit static-instance policy")


def apply_aliases(font: TTFont, requested: set[int]) -> list[dict[str, object]]:
    cmap = font.getBestCmap() or {}
    records = []
    for cp, (fallback, reason) in sorted(GLYPH_ALIASES.items()):
        if cp not in requested or cp in unicode_cmap(font):
            continue
        if fallback not in unicode_cmap(font):
            continue
        name = cmap[fallback]
        for table in font["cmap"].tables:
            if table.isUnicode() and table.format in (4, 12):
                table.cmap[cp] = name
        records.append({"codepoint": f"U+{cp:04X}", "fallback": f"U+{fallback:04X}", "reason": reason})
    return records


def icon_points(points: set[int]) -> set[int]:
    """The Private Use Area subset of `points`, i.e. the interface icons."""
    return {cp for cp in points if ICON_FIRST <= cp <= ICON_LAST}


def merge_icons(target: TTFont, icon_data: bytes, points: set[int]) -> dict[str, object]:
    """Add outline glyphs for `points` from the icon font into `target`.

    Only glyphs reachable from the requested codepoints are copied, layout
    features are dropped so the font's name-to-icon ligatures and the Latin
    glyphs that drive them never enter the image, and the icon em square is
    scaled to the text font's so both faces share one coordinate system.
    Glyph names are prefixed, so a copied glyph can never take over a text one.
    """
    with TTFont(io.BytesIO(icon_data), lazy=False, recalcTimestamp=False) as icons:
        if (icons.flavor is not None or icons.sfntVersion != "\x00\x01\x00\x00"
                or "glyf" not in icons or "loca" not in icons):
            raise FontBuildError("icon source must be uncompressed sfnt TrueType with glyf outlines")
        axes: dict[str, list[float]] = {}
        if "fvar" in icons:
            axes = {a.axisTag: [a.minValue, a.defaultValue, a.maxValue] for a in icons["fvar"].axes}
            unpinned = sorted(set(axes) - set(ICON_INSTANCE))
            if unpinned:
                raise FontBuildError(f"icon font has axes with no pinned value: {unpinned}")
            instancer.instantiateVariableFont(icons, ICON_INSTANCE, inplace=True, updateFontNames=False)
            if any(t in icons for t in ("fvar", "gvar", "avar", "HVAR")):
                raise FontBuildError("icon font did not reduce to a static instance")
        missing = points - unicode_cmap(icons)
        if missing:
            raise FontBuildError("icon font lacks required UI glyphs: " +
                                 ", ".join(f"U+{cp:04X}" for cp in sorted(missing)))
        options = subset.Options()
        options.flavor = None
        options.hinting = True
        options.layout_features = []  # no ligatures, hence no Latin icon-name glyphs
        options.notdef_glyph = options.notdef_outline = True
        options.recalc_timestamp = False
        options.canonical_order = True
        options.drop_tables += ["STAT", "DSIG"]
        worker = subset.Subsetter(options=options)
        worker.populate(unicodes=sorted(points))
        worker.subset(icons)
        source_upem = icons["head"].unitsPerEm
        upem = target["head"].unitsPerEm
        if source_upem != upem:
            scale_upem(icons, upem)
        icon_cmap = icons.getBestCmap()
        icon_glyf, icon_hmtx = icons["glyf"], icons["hmtx"]
        # A composite icon references other glyphs; copy those too or it renders
        # as a hole. subset already kept them, they are simply not in the cmap.
        wanted = {icon_cmap[cp] for cp in points}
        pending = sorted(wanted)
        while pending:
            glyph = icon_glyf[pending.pop()]
            if glyph.isComposite():
                for part in glyph.components:
                    if part.glyphName not in wanted:
                        wanted.add(part.glyphName)
                        pending.append(part.glyphName)
        order = target.getGlyphOrder()
        taken = set(order)
        rename: dict[str, str] = {}
        for name in sorted(wanted):
            candidate = f"icon.{name}"
            while candidate in taken:
                candidate = f"_{candidate}"
            rename[name] = candidate
            taken.add(candidate)
        glyf, hmtx = target["glyf"], target["hmtx"]
        for name in sorted(wanted):
            glyph = icon_glyf[name]
            if glyph.isComposite():
                for part in glyph.components:
                    part.glyphName = rename[part.glyphName]
            glyf.glyphs[rename[name]] = glyph
            hmtx.metrics[rename[name]] = icon_hmtx.metrics[name]
        # MiSans also carries vertical metrics, for CJK set in columns. MixOS
        # never sets type vertically, but every table indexed by glyph has to
        # describe every glyph or the font cannot be compiled at all.
        if "vmtx" in target:
            for name in sorted(wanted):
                target["vmtx"].metrics[rename[name]] = (upem, 0)
        target.setGlyphOrder(order + [rename[name] for name in sorted(wanted)])
        glyf.glyphOrder = target.getGlyphOrder()
        target["maxp"].numGlyphs = len(target.getGlyphOrder())
        for table in target["cmap"].tables:
            if table.isUnicode() and table.format in (4, 12):
                for cp in sorted(points):
                    table.cmap[cp] = rename[icon_cmap[cp]]
        target["maxp"].recalc(target)
        if "OS/2" in target:  # bit 60 advertises the Private Use Area
            target["OS/2"].ulUnicodeRange2 |= 1 << (60 - 32)
        return {
            "source_sha256": hashlib.sha256(icon_data).hexdigest(), "source_bytes": len(icon_data),
            "family": icons["name"].getDebugName(1), "version": icons["name"].getDebugName(5),
            "variable_axes": axes, "pinned_instance": ICON_INSTANCE,
            "layout_features": "dropped (no ligatures, no Latin name glyphs)",
            "source_units_per_em": source_upem, "scaled_to_units_per_em": upem,
            "codepoints": [f"U+{cp:04X}" for cp in sorted(points)],
            "glyphs_copied": len(wanted), "glyph_name_prefix": "icon.",
        }


def verify_pillow(data: bytes, required: set[int]) -> dict[str, object]:
    """Actually load and rasterize through Pillow's local FreeType engine."""
    rasterized = 0
    terminal_overflow = []
    for size in UI_SIZES:
        face = ImageFont.truetype(io.BytesIO(data), size=size, layout_engine=ImageFont.Layout.BASIC)
        for cp in sorted(required):
            text = chr(cp)
            mask = face.getmask(text, mode="L")
            if not text.isspace() and unicodedata.category(text) not in ("Cf", "Cc") and not mask.getbbox():
                raise FontBuildError(f"FreeType produced an empty required glyph U+{cp:04X} at {size}px")
            rasterized += 1
            # Terminal cells only ever carry text. An icon is drawn by the
            # launcher and status bar at its own size, so measuring it against
            # the 20 px cell would report a clipping risk that cannot occur.
            if size == 20 and not (ICON_FIRST <= cp <= ICON_LAST):
                left, top, right, bottom = face.getbbox(text, anchor="la")
                advance = round(face.getlength(text))
                if left < 0 or right > min(max(advance, 1), 32) or top < 0 or bottom > 24:
                    terminal_overflow.append({"codepoint": f"U+{cp:04X}", "bbox": [left, top, right, bottom],
                                              "advance": advance})
    # Firmware passes the full mapped partition length to FT_New_Memory_Face.
    padded = data + b"\xff" * (FONT_PARTITION_BYTES - len(data))
    ImageFont.truetype(io.BytesIO(padded), 20, layout_engine=ImageFont.Layout.BASIC).getmask("A", mode="L")
    return {"engine": "Pillow FreeType / BASIC (no shaping)", "pillow_version": pillow_version,
            "freetype_version": features.version_module("freetype2"), "sizes_px": list(UI_SIZES),
            "required_glyph_size_checks": rasterized, "partition_padded_load": "passed",
            "terminal_20px_potential_clipping": terminal_overflow,
            "scope": "Host rasterization only; terminal bbox diagnostic is not target framebuffer validation."}


def build(source: Path, output: Path, manifest: Path, *, ui_source: Path = DEFAULT_UI,
          icon_source: Path | None = None) -> dict[str, object]:
    # Same rule as ui_source: the real project layout is the default, so callers
    # that do not care about icons still build the image the firmware expects.
    if icon_source is None and DEFAULT_ICONS.exists():
        icon_source = DEFAULT_ICONS
    resolved = validate_paths(source, output, manifest, ui_source, icon_source)
    source, output, manifest, ui_source = resolved[:4]
    icon_source = resolved[4] if icon_source is not None else None
    # Invalidate older generated results before any work; failed rebuilds cannot
    # masquerade as successful current builds. Unsafe alias calls change nothing.
    manifest.unlink(missing_ok=True)
    output.unlink(missing_ok=True)
    temps: list[Path] = []
    try:
        source_data = source.read_bytes()  # Source is only ever opened read-only.
        source_hash = hashlib.sha256(source_data).hexdigest()
        ui_data = ui_source.read_bytes()
        ui_points, literals = extract_ui(ui_data.decode("utf-8-sig"))
        required = BASIC_LATIN | ui_points | set(map(ord, EXTRA_TEXT)) | RUNTIME_REQUIRED
        # Icons are satisfied by the icon face, text by the source face. Asking
        # MiSans for a Private Use Area glyph would fail the coverage check for
        # a glyph it is not supposed to have.
        icons_required = icon_points(required)
        text_required = required - icons_required
        icon_data = b""
        if icons_required:
            if icon_source is None:
                raise FontBuildError(
                    "UI uses Private Use Area icons (" +
                    ", ".join(f"U+{cp:04X}" for cp in sorted(icons_required)) +
                    ") but no icon font was given")
            icon_data = icon_source.read_bytes()
        gb = gb2312_codepoints()
        requested = text_required | LATIN_1 | gb
        icon_record: dict[str, object] | None = None
        with TTFont(io.BytesIO(source_data), lazy=False, recalcTimestamp=False) as original:
            require_truetype(original)
            native = unicode_cmap(original)
            aliases = apply_aliases(original, requested)
            available = unicode_cmap(original)
            missing_required = text_required - available
            if missing_required:
                raise FontBuildError("source font lacks required MixOS glyphs: " +
                                     ", ".join(f"U+{cp:04X}" for cp in sorted(missing_required)))
            selected = requested & available
            options = subset.Options()
            options.flavor = None
            options.hinting = True
            options.layout_features = ["*"]
            options.name_IDs = ["*"]
            options.name_languages = ["*"]
            options.name_legacy = True
            options.notdef_glyph = options.notdef_outline = True
            options.recalc_average_width = options.recalc_max_context = True
            options.canonical_order = True
            options.recalc_timestamp = False
            worker = subset.Subsetter(options=options)
            worker.populate(unicodes=sorted(selected))
            worker.subset(original)
            # After subsetting, so the text glyph order is the one the subsetter
            # chose and the icons are simply appended to it.
            if icons_required:
                icon_record = merge_icons(original, icon_data, icons_required)
            original["head"].created = original["head"].modified = FIXED_SFNT_TIME
            memory = io.BytesIO()
            original.save(memory, reorderTables=True)
            data = memory.getvalue()
        if not 0 < len(data) <= FONT_PARTITION_BYTES:
            raise FontBuildError(f"subset is {len(data)} bytes; partition limit is {FONT_PARTITION_BYTES}")
        if data[:4] != b"\x00\x01\x00\x00":
            raise FontBuildError("generated image is not raw sfnt TrueType")
        with TTFont(io.BytesIO(data), lazy=False, recalcTimestamp=False) as verified:
            require_truetype(verified)
            output_cmap = unicode_cmap(verified)
            if selected - output_cmap:
                raise FontBuildError("generated font lost requested Unicode mappings")
            if icons_required - output_cmap:
                raise FontBuildError("generated font lost merged icon mappings: " +
                                     ", ".join(f"U+{cp:04X}" for cp in sorted(icons_required - output_cmap)))
            # FreeType normally selects a Windows Unicode charmap; verify each
            # supported full-BMP mapping, not merely the fontTools preferred one.
            for table in verified["cmap"].tables:
                if table.isUnicode() and table.format in (4, 12):
                    needed = {cp for cp in required if table.format == 12 or cp <= 0xFFFF}
                    if any(table.cmap.get(cp, ".notdef") == ".notdef" for cp in needed):
                        raise FontBuildError("generated Unicode cmap subtable lacks required glyphs")
            family, style, version = (verified["name"].getDebugName(n) for n in (1, 2, 5))
            glyphs = verified["maxp"].numGlyphs
        load_check = verify_pillow(data, required)
        if sha256(source) != source_hash or ui_source.read_bytes() != ui_data:
            raise FontBuildError("source font or UI source changed during build")
        if icons_required and icon_source.read_bytes() != icon_data:
            raise FontBuildError("icon font changed during build")
        record: dict[str, object] = {
            "schema": 2, "status": "verified", "format": "raw sfnt TrueType / glyf+loca / single face",
            "sfnt_signature_hex": "00010000", "head_timestamp_utc": "2000-01-01T00:00:00Z",
            "source": str(source), "source_sha256": source_hash, "source_bytes": len(source_data),
            "family": family, "style": style, "version": version, "fonttools_version": fontTools.__version__,
            "ui_source": str(ui_source), "ui_source_sha256": hashlib.sha256(ui_data).hexdigest(),
            "ui_extraction": "All printable UTF-8 C string literals, conservative superset; comments excluded; no macro expansion.",
            "ui_literal_count": len(literals), "ui_literals": literals,
            "ui_codepoints": len(ui_points), "ui_non_ascii_codepoints": len(ui_points - BASIC_LATIN),
            "ui_coverage_missing": codepoint_records(ui_points - output_cmap),
            "required_codepoints": len(required), "required_coverage_missing": codepoint_records(required - output_cmap),
            "icon_codepoints": len(icons_required), "icon_source": str(icon_source) if icon_source else None,
            "icons": icon_record,
            "requested_codepoints": len(requested), "selected_requested_codepoints": len(selected),
            "included_codepoints": len(output_cmap), "glyphs": glyphs,
            "subset_additional_mappings": codepoint_records(output_cmap - selected),
            "gb2312_requested": len(gb), "gb2312_included": len(gb & output_cmap),
            "source_missing_requested": codepoint_records(requested - native), "glyph_aliases": aliases,
            "source_missing_optional_codepoints": len(requested - available),
            "omitted_optional": codepoint_records(requested - available),
            "missing_policy": "ASCII, actual UI, EXTRA_TEXT and U+FFFD must exist (or use a listed alias); fail otherwise. Unavailable non-UI GB2312/Latin-1 are listed, never claimed covered. Unlisted fallback substitution is forbidden.",
            "included_unicode": [f"U+{cp:04X}" for cp in sorted(output_cmap)],
            "output": str(output), "output_sha256": hashlib.sha256(data).hexdigest(), "output_bytes": len(data),
            "partition_offset": FONT_PARTITION_OFFSET, "partition_bytes": FONT_PARTITION_BYTES,
            "partition_spare_bytes": FONT_PARTITION_BYTES - len(data), "load_verification": load_check,
        }
        # Only verified bytes are staged, then publish the manifest last as the
        # completion marker. Catch failures (including Ctrl-C), remove both.
        for target, payload in ((output, data), (manifest, (json.dumps(record, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))):
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
            temp = Path(name)
            temps.append(temp)
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        os.replace(temps[0], output)
        os.replace(temps[1], manifest)
        return record
    except BaseException:
        manifest.unlink(missing_ok=True)
        output.unlink(missing_ok=True)
        raise
    finally:
        for temp in temps:
            temp.unlink(missing_ok=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="read-only source raw .ttf")
    parser.add_argument("output", type=Path, help="generated subset .ttf (previous result invalidated on rebuild)")
    parser.add_argument("--manifest", type=Path, help="JSON manifest (default: OUTPUT.manifest.json)")
    parser.add_argument("--ui-source", type=Path, default=DEFAULT_UI, help="UTF-8 C UI source (default: actual mix_ui.c)")
    parser.add_argument("--icons", type=Path, default=None,
                        help=f"icon font supplying the UI's Private Use Area glyphs (default: {DEFAULT_ICONS})")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    manifest = args.manifest or args.output.with_suffix(args.output.suffix + ".manifest.json")
    try:
        record = build(args.source, args.output, manifest, ui_source=args.ui_source, icon_source=args.icons)
    except (FontBuildError, OSError, ValueError, TTLibError) as exc:
        print(f"font build failed: {exc}", file=sys.stderr)
        return 1
    # ASCII-escaped console JSON also works in legacy Windows code pages.
    print(json.dumps({k: record[k] for k in ("status", "output", "output_sha256", "output_bytes",
                     "required_codepoints", "included_codepoints", "icon_codepoints", "gb2312_included",
                     "glyph_aliases", "source_missing_optional_codepoints")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
