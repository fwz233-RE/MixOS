#!/usr/bin/env python3
"""Measure what the deck's microphones are actually producing.

Both interfaces report "没听到声音，离麦克风近一点再说", which linux/apps/audio.py
emits when too_quiet(peak) is true, i.e. the peak of the selected channel is at
or below SILENCE_PEAK = 0.05. That threshold is calibrated against the ES8389
microphone PGA sitting at 36 dB, where a voice peaks near 0.29 and a silent room
near 0.03.

Three different faults produce the same sentence on screen and they need
different fixes, so this separates them by measurement rather than by guessing:

  * the capture is a dead line (every sample identical) - the codec is not
    driving I2S, and no amount of speaking helps;
  * the capture carries the room but the voice is genuinely below threshold -
    the analogue gain is wrong, i.e. the running firmware is not the one the
    thresholds were calibrated against;
  * the capture is fine and the fault is downstream, in the recogniser.

It reports both channels separately because audio.py deliberately keeps only
one (MIXOS_VOICE_CHANNEL, default 0) and the two differ by about 9 dB on this
board.
"""
from __future__ import annotations

import argparse
import array
import subprocess
import sys

DEVICE = 'plughw:CARD=UACCDC,DEV=0'
RATE = 16000
CHANNELS = 2

# Copied from linux/apps/audio.py so this probe reports the same verdicts the
# interfaces reach. If they drift, the probe is wrong and should be corrected.
SILENCE_PEAK = 0.05
DEAD_SPAN = 2
NOISE_RMS = 0.008
TARGET_RMS = 0.08


def capture(seconds: float, device: str) -> bytes:
    command = ['arecord', '-q', '-D', device, '-f', 'S16_LE',
               '-r', str(RATE), '-c', str(CHANNELS), '-t', 'raw',
               '-d', str(int(seconds))]
    done = subprocess.run(command, capture_output=True)
    if done.returncode != 0:
        sys.exit(f'arecord failed: {done.stderr.decode("utf-8", "replace").strip()}')
    return done.stdout


def channel(samples: array.array, index: int) -> array.array:
    usable = len(samples) - (len(samples) % CHANNELS)
    return samples[index:usable:CHANNELS]


def describe(name: str, one: array.array) -> None:
    if not one:
        print(f'  {name}: no samples')
        return
    peak = max(max(one), -min(one)) / 32768.0
    span = max(one) - min(one)
    rms = (sum(s * s for s in one) / len(one)) ** 0.5 / 32768.0
    verdict = []
    if span <= DEAD_SPAN:
        verdict.append(f'DEAD LINE (span {span} counts, every sample ~identical)')
    elif peak <= SILENCE_PEAK:
        verdict.append(f'below SILENCE_PEAK {SILENCE_PEAK}: reported as "too quiet"')
    else:
        verdict.append('above threshold: would be sent for recognition')
    print(f'  {name}: peak {peak:.4f}  rms {rms:.5f}  span {span:6d} counts '
          f'min {min(one):6d} max {max(one):6d}')
    print(f'       {verdict[0]}')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=float, default=5.0)
    parser.add_argument('--device', default=DEVICE)
    parser.add_argument('--label', default='')
    args = parser.parse_args()

    if args.label:
        print(f'== {args.label}')
    print(f'capturing {args.seconds:g}s from {args.device}')
    pcm = capture(args.seconds, args.device)
    samples = array.array('h')
    samples.frombytes(pcm[:len(pcm) - (len(pcm) % 2)])
    print(f'  {len(samples)} samples total ({len(pcm)} bytes)')
    if not samples:
        print('  nothing captured')
        return 1
    describe('FL (channel 0, the one recordings keep)', channel(samples, 0))
    describe('FR (channel 1, discarded)', channel(samples, 1))
    return 0


if __name__ == '__main__':
    sys.exit(main())
