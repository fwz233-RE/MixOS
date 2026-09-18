/* SPDX-License-Identifier: MIT
 * Protected, independently read-back and boot-verified 2026-09-16 baseline.
 * This guard is not image authentication; the maintenance host is trusted.
 */
#pragma once
#define MIX_BASELINE_BYTES 894560u
#define MIX_BASELINE_SLOT 0u
#define MIX_BASELINE_SHA_HEX "7875d9a513acb95463b72e785ebd160c70d03f85e965c3a30a93d954bb4cff5f"
#define MIX_BASELINE_ELF_HEX "cfacb3fe25931e918b8f46d1f840da8d57ba39ef799bd0af4629939b9185761a"
static const unsigned char MIX_BASELINE_SHA[32] = {
    0x78,0x75,0xd9,0xa5,0x13,0xac,0xb9,0x54,0x63,0xb7,0x2e,0x78,0x5e,0xbd,0x16,0x0c,
    0x70,0xd0,0x3f,0x85,0xe9,0x65,0xc3,0xa3,0x0a,0x93,0xd9,0x54,0xbb,0x4c,0xff,0x5f
};
