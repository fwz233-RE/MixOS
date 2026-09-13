# Source provenance

| Component | Source | Baseline | License record |
|---|---|---|---|
| ESP32-S3 firmware | `TypixDeck-esp32s3-firmware` | `1f29c50` | See copied repository license files |
| STM32 keyboard firmware | `TypixDeck-keyboard-firmware` | `a1694e4` | See copied repository license files |
| ESP-IDF | Espressif ESP-IDF | `v5.4.2` | Apache-2.0, upstream `LICENSE` |
| QMK | QMK firmware | `0.28.0` API compatibility target | GPL-2.0, upstream `license.txt` |
| Optional MixOS display font | User-supplied `D:/AI/MiSans-Normal.ttf` | MiSans 4.003, SHA-256 `1a5f4112daaa9473747c6834041646cc9b2c338cb40ab5dbb2f0161f8968ca10` | No license metadata is embedded in this TTF; retain and verify the supplier's separate license before redistribution |

MixOS firmware directories are independent clones with their remotes removed. Dependency versions and hashes remain in the ESP component manifest/lock files. The original repositories and existing root files are outside the scope of this project and must stay unchanged.

The bundled font remains subject to the license and attribution files shipped with its source repository. Verify those notices before redistribution.
