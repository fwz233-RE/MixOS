# Source provenance

| Component | Source | Baseline | License record |
|---|---|---|---|
| ESP32-S3 firmware | `TypixDeck-esp32s3-firmware` | `1f29c50` | See copied repository license files |
| STM32 keyboard firmware | `TypixDeck-keyboard-firmware` | `a1694e4` | See copied repository license files |
| ESP-IDF | Espressif ESP-IDF | `v5.4.2` | Apache-2.0, upstream `LICENSE` |
| QMK | QMK firmware | `0.28.0` API compatibility target | GPL-2.0, upstream `license.txt` |
| Optional MixOS display font | User-supplied `D:/AI/MiSans-Normal.ttf` | MiSans 4.003, SHA-256 `1a5f4112daaa9473747c6834041646cc9b2c338cb40ab5dbb2f0161f8968ca10` | No license metadata is embedded in this TTF; retain and verify the supplier's separate license before redistribution |
| Translator backend | `google-gemma/gemma-translator` | `47f9b3ba40ca3650fb80ee42264a76d6a2b5f8ba` (2026-08-14) | Apache-2.0, upstream `LICENSE` copied alongside |
| Terminal input method | `adam-ikari/term-ime` | `v1.0.9` (`1fcb7ae2`) | Upstream repository; fetched, patched and built by `tools/stage_ime.py` + `tools/build_ime_remote.py`, digests in `build/ime/manifest.json` |

`linux/apps/translator/vendor/` holds byte-exact copies of three upstream files
(`backend/server.py`, `backend/requirements.txt` and `LICENSE`) with their
SHA-256 digests recorded in `linux/apps/translator/vendor/PROVENANCE.json`.
Those files are never edited. Every difference between upstream and this device
— binding to loopback instead of every interface, and not pre-loading the
speech models on a 4 GiB machine — lives in
`linux/apps/translator/service.py`, which imports them. Re-verify the copies
against the pinned commit before changing them.

## term-ime

`term-ime` is still not copied into this tree; it is fetched on demand into
`build/ime/`, which is not tracked. What *is* tracked is the complete pin set and
every change made to the fetched source, in `tools/stage_ime.py`. A run records
the digest of each tarball, and the digest of each edited file before and after,
in `build/ime/manifest.json`.

Fifteen repositories are pinned: term-ime itself and its eight submodules, plus
six of librime's own. They are fetched as tarballs from `codeload.github.com`
rather than cloned, because `github.com:443` does not answer from either the
Windows host or the device — measured 2026-09-14, while `codeload`,
`raw.githubusercontent.com` and `api.github.com` all answer normally.
`tools/stage_ime.py --verify-pins` re-reads every gitlink through the GitHub API
and reports any that moved.

Seven files are changed, each recorded with a reason:

* six in librime, which use `std::any_of`, `std::upper_bound`, `std::all_of`,
  `std::partial_sort`, `std::find` and `std::stable_sort` without including
  `<algorithm>`. GCC 14's libstdc++ no longer supplies it transitively, so on
  Debian 13 the build stops at the first of them. `--scan-includes` finds these
  in one pass and is what to re-run after moving a pin.
* one in term-ime, `src/ui/renderer.cpp`. `Renderer::init` enables raw mode by
  clearing three `c_lflag` bits and leaves `IXON` set, so XON/XOFF flow control
  consumes Ctrl-S and Ctrl-Q in the line discipline and no program running
  inside term-ime ever receives them. In the notes editor those are Save and
  Back. `linux/mixosd.py` also clears the same bits on the session terminal it
  owns, which fixes the device path independently of this edit.

Four directories are fetched and then dropped before packing — `deps/sml/doc`,
`deps/json/docs`, `deps/json/tests` and term-ime's `website` — because no CMake
target reads them and they are 92 MB on a link that runs at 0.20 MB/s. Two of
leveldb's submodules are never fetched at all, because leveldb is configured
with its tests and benchmarks off, which is what guards the `add_subdirectory`
calls that would need them. `tools/stage_ime.py --explain` prints all of this.

The notes launcher starts term-ime when the binary and its rime data are both
present and runs the editor directly when they are not.

MixOS firmware directories are independent clones with their remotes removed. Dependency versions and hashes remain in the ESP component manifest/lock files. The original repositories and existing root files are outside the scope of this project and must stay unchanged.

The bundled font remains subject to the license and attribution files shipped with its source repository. Verify those notices before redistribution.
