#!/usr/bin/env python3
"""Ask the deployed audio module what it would say about a real capture.

The interfaces reported "没听到声音，离麦克风近一点再说" after the fix that is
supposed to make them report NO_MICROPHONE instead. Two different things can
produce that, and reading the source cannot tell them apart:

  * the running code is older than the source on the development machine, or
  * the new code is running and its ordering is still wrong.

So this imports the module from /opt/mixos rather than from a checkout, feeds
it a capture taken on the spot, and prints the branch the interfaces would take
for it. Whatever it prints is what the screen would say.
"""
from __future__ import annotations

import argparse
import importlib.util
import subprocess
import sys
from pathlib import Path

DEVICE = 'plughw:CARD=UACCDC,DEV=0'


def load(path: Path):
    spec = importlib.util.spec_from_file_location('deployed_audio', path)
    if spec is None or spec.loader is None:
        sys.exit(f'cannot import {path}')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def capture(seconds: float, device: str, rate: int, channels: int) -> bytes:
    done = subprocess.run(
        ['arecord', '-q', '-D', device, '-f', 'S16_LE', '-r', str(rate),
         '-c', str(channels), '-t', 'raw', '-d', str(int(seconds))],
        capture_output=True)
    if done.returncode != 0:
        sys.exit(f'arecord failed: {done.stderr.decode("utf-8", "replace").strip()}')
    return done.stdout


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--module', default='/opt/mixos/linux/apps/audio.py')
    parser.add_argument('--seconds', type=float, default=5.0)
    parser.add_argument('--device', default=DEVICE)
    args = parser.parse_args()

    path = Path(args.module)
    audio = load(path)
    print(f'module   : {path}')
    print(f'has no_signal: {hasattr(audio, "no_signal")}')
    print(f'SILENCE_PEAK={getattr(audio, "SILENCE_PEAK", None)} '
          f'DEAD_SPAN={getattr(audio, "DEAD_SPAN", None)}')

    rate = getattr(audio, 'SAMPLE_RATE', 16000)
    channels = getattr(audio, 'CHANNELS', 2)
    print(f'capturing {args.seconds:g}s at {rate}Hz x{channels}')
    pcm = capture(args.seconds, args.device, rate, channels)
    print(f'captured {len(pcm)} bytes')

    peak, rms = audio.levels(pcm)
    print(f'levels   : peak={peak:.4f} rms={rms:.5f}')

    dead = audio.no_signal(pcm) if hasattr(audio, 'no_signal') else None
    quiet = audio.too_quiet(peak)
    print(f'no_signal: {dead}')
    print(f'too_quiet: {quiet}')

    # The order the interfaces use; see translator/app.py _work.
    print('--- what the screen would say ---')
    if dead:
        print(getattr(audio, 'NO_MICROPHONE', 'NO_MICROPHONE missing'))
    elif quiet:
        print('没听到声音，离麦克风近一点再说')
    else:
        print('(sent for recognition)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
