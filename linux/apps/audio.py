"""Recording and playback on the ESP32's USB audio device.

The microphone and the speaker are not on this machine. They are on the
ESP32-S3 on the other end of the USB cable, which enumerates as a composite
device: a serial port carrying the terminal, and an audio interface carrying
sound. Linux sees the audio half as an ordinary USB audio card.

Why ALSA through ``arecord`` and ``aplay`` rather than a Python audio library.
Three reasons, in order of how much they matter:

1. ``sounddevice`` needs PortAudio and a compiled wheel. The speech models
   already live in their own virtual environment; the interfaces do not, and
   should keep running on the system ``python3`` with nothing installed.
2. ``arecord`` is already on the machine and already works with this card. A
   failure is visible on the command line before any of this code runs, which
   is worth more during bring-up than a tidier API.
3. Recording is stopped by a keypress, and a subprocess can be killed. A
   callback-driven library would have to be woken and unwound instead.

Why the card is named and not numbered. The inventory found this card at index
2, behind two HDMI audio devices. Card numbers are assigned in probe order:
plug in a monitor, or let the ESP32 re-enumerate after a reset, and index 2
becomes something else while ``UACCDC`` stays ``UACCDC``. ``plughw`` rather than
``hw`` so that ALSA converts the rate: the recogniser wants 16 kHz and the
speech synthesiser emits whatever it emits, and neither rate is necessarily one
the ESP32's descriptor offers.
"""
from __future__ import annotations

import array
import os
import shutil
import subprocess
import sys
import threading
import time

BIG_ENDIAN = sys.byteorder == 'big'

# Named, not numbered: see the module docstring.
DEFAULT_DEVICE = os.environ.get('MIXOS_AUDIO_DEVICE', 'plughw:CARD=UACCDC,DEV=0')
SAMPLE_RATE = 16000        # what the speech recogniser is trained on
CHANNELS = 1               # what a finished recording is, whatever was captured
SAMPLE_BYTES = 2           # S16_LE
READ_BLOCK = 4096          # 1024 stereo frames; a whole number of frames either way

# ---------------------------------------------------------------------------
# Two microphones, one of which is not worth listening to
#
# This board carries two MEMS microphones on the ES8389's two ADC channels
# (firmware/esp32s3/main/audio.c), and the card offers capture only as stereo:
#
#     CHANNELS: 2      RATE: 48000      FORMAT: S16_LE
#
# Asking ALSA for one channel therefore does not select a microphone, it
# averages both. Measured on typixdeck on 2026-09-14, three takes of room noise
# and three of the same sentence through the deck's own speaker:
#
#     channel   noise rms   signal rms   signal-to-noise
#     FL        0.00200     0.00518        +8.2 dB
#     FR        0.00511     0.00464        -0.8 dB
#
# The right-hand microphone hears its own noise as loudly as it hears a voice.
# Averaging it with the left one drags a usable +8 dB channel down towards -1 dB,
# and the transcripts say so plainly - the same three takes, each channel put
# through this module's own level correction:
#
#     FL  "今天天气很好，我们一起去公园"        (and two near misses)
#     FR  fragments and invented syllables
#     averaged   nothing at all
#
# So the recording takes one microphone and discards the other. This is worth
# about 9 dB of signal-to-noise over what ALSA's average produced, which is the
# largest single improvement available anywhere in this path, and it costs
# nothing: the second channel was never carrying anything useful.
#
# MIXOS_VOICE_CHANNEL exists because a quiet right channel may be particular to
# this unit - firmware/esp32s3/main/board_pins.h already carries one workaround
# for a dead right-hand analogue front end on board #1 - so a differently
# behaved board is a deployment setting rather than a code change.
CAPTURE_CHANNELS = 2
VOICE_CHANNEL = int(os.environ.get('MIXOS_VOICE_CHANNEL', '0'))
# A recording nobody stopped must not grow without limit. Two minutes of
# 16 kHz mono is under 4 MB, and is already far longer than anything the
# recogniser handles well in one pass.
MAX_SECONDS = 120.0

# ---------------------------------------------------------------------------
# Level, and why the recordings still have to be scaled before recognition
#
# Moonshine answers audio that is too quiet with an empty transcript rather than
# an error. On screen an empty transcript is indistinguishable from broken speech
# recognition, which is exactly how this was reported: every request HTTP 200, no
# text on the screen, and a translator that appeared to do nothing because it
# never had a sentence to translate.
#
# Three separate things made recordings too quiet, and all three had to be fixed.
# In the order of how much each was worth:
#
#   1. The average of two microphones instead of one. Worth about 9 dB of
#      signal-to-noise. See the capture constants above.
#   2. The ES8389 microphone PGA, which sat at 24 dB while the part supports
#      36.5 dB. Raising it to 36 dB in firmware/esp32s3/main/audio.c moved the
#      captured peak of a voice from 0.06-0.08 to 0.29 - a little over 12 dB -
#      and it clips nothing: 0.29 leaves 10 dB before full scale.
#   3. What is left, corrected here. Even at 36 dB a voice arrives at an rms of
#      about 0.022 where the recogniser wants roughly 0.08, so recordings are
#      still scaled, per recording, where how quiet this one actually was is
#      known.
#
# The order matters because only the first two improve signal-to-noise. Scaling
# here multiplies the room along with the voice, so it can make a recording loud
# enough to be read but never clearer. That is why the earlier attempt to fix
# this in software alone got partial transcripts - "去很好，我们一起去公园" for
# "今天天气很好，我们一起去公园散步吧" - and why the analogue gain was worth
# changing despite an earlier note here arguing against it. That note reasoned
# from a microphone clipping while held against the deck's own speaker at full
# volume, which is not a condition anybody uses the device in.
#
# The card exposes no capture control (`amixer -c UACCDC contents` offers only
# PCM Playback), so item 2 is reachable only from the firmware side; there is
# nothing to turn up on the Linux side of the USB cable.
TARGET_RMS = 0.08          # ordinary speech, loud enough for the recogniser
CEILING_PEAK = 0.95        # scaling never clips: the peak is a hard limit
# The room, measured on the left microphone with the PGA at 36 dB: three takes of
# a silent room came in at an rms of 0.0072 to 0.0075. Only the level bar uses
# this - it is where the bar's empty end is - and it is measured rather than
# guessed so that silence draws nothing. See meter().
NOISE_RMS = 0.008
# Below this peak there is no voice in the recording, only the room. Scaling
# that up hands the recogniser amplified noise, which it answers with invented
# words - worse than answering with nothing.
#
# Calibrated against the left microphone with the PGA at 36 dB, which is what
# recordings now go through. Three takes of room noise on it peaked at 0.027,
# 0.031 and 0.031; three takes containing a voice peaked at 0.297, 0.286 and
# 0.291. The threshold sits in that gap with about 4 dB of margin above the room
# and 15 dB below a voice.
#
# It has been calibrated twice before, and both earlier values are now wrong for
# a reason worth recording, because both produced the same visible symptom from
# opposite directions:
#
#   0.012 against the averaged microphone pair, where the room measured 0.0118.
#   A quiet voice fell under the threshold, the gain came out at 1.0, and
#   Moonshine answered audio it could not hear with an empty string. Every
#   request HTTP 200, nothing on the screen.
#
#   0.012 again after the PGA went to 36 dB, where the room peaks at 0.03. Now
#   the room sits above the threshold: silence is treated as speech, amplified
#   eleven-fold, and the interface reports "nothing was recognised" when the
#   honest answer is that nobody said anything.
#
# So this constant is tied to the analogue gain in
# firmware/esp32s3/main/audio.c and to the channel selection above. Changing
# either means measuring the room again.
SILENCE_PEAK = 0.05
MAX_GAIN = 32.0            # 30 dB; beyond this only the noise is getting louder
# How many samples the loudness estimate looks at. Enough to be accurate,
# bounded so that a two-minute recording costs the same as a two-second one.
RMS_SAMPLES = 20000


class AudioUnavailable(RuntimeError):
    """The capture or playback device is not usable, with the reason attached."""


def _tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise AudioUnavailable(
            f'{name} is not installed; install the alsa-utils package')
    return path


def describe_device(device: str = DEFAULT_DEVICE) -> str:
    """A short, true sentence about whether sound will work, for the UI.

    Called before anything is recorded so a missing device is a line of text on
    the screen rather than a subprocess that dies half a second later.
    """
    try:
        arecord = _tool('arecord')
    except AudioUnavailable as exc:
        return str(exc)
    try:
        listing = subprocess.run([arecord, '-l'], capture_output=True, text=True,
                                 timeout=5).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        return f'could not list capture devices: {exc}'
    card = device.split('CARD=', 1)[1].split(',', 1)[0] if 'CARD=' in device else device
    if card in listing:
        return f'capture on {card}'
    return (f'no capture device named {card}; the ESP32 may be disconnected or '
            f'running firmware without USB audio')


def pcm16_to_float32(pcm: bytes) -> bytes:
    """Signed 16-bit samples to the float32 buffer the backend expects.

    The backend's ``/api/stt`` takes the bytes of a browser ``Float32Array``,
    little-endian, normalised to [-1, 1]. This is a faithful conversion and
    changes no levels; ``for_recognition`` is what recordings go through.
    """
    return _to_float32(_samples(pcm), 1.0)


def _samples(pcm: bytes) -> array.array:
    """The PCM as host-order signed 16-bit samples, trailing odd byte dropped."""
    samples = array.array('h')
    samples.frombytes(pcm[:len(pcm) - (len(pcm) % 2)])
    if BIG_ENDIAN:
        samples.byteswap()
    return samples


def _to_float32(samples: array.array, gain: float) -> bytes:
    """Samples to the little-endian float32 buffer, scaled in the same pass.

    Done without numpy so the interfaces keep no dependency the system Python
    does not already have, and in one comprehension so applying a gain costs
    nothing over not applying one.
    """
    scale = gain / 32768.0
    floats = array.array('f', [s * scale for s in samples])
    if BIG_ENDIAN:
        floats.byteswap()
    return floats.tobytes()


def voice_channel(pcm: bytes, channels: int = CAPTURE_CHANNELS,
                  channel: int = VOICE_CHANNEL) -> bytes:
    """One microphone's samples out of an interleaved capture.

    Selecting rather than mixing. See the measurements above the capture
    constants: the two microphones on this board differ by 9 dB of
    signal-to-noise, so combining them is worse than ignoring one.

    A capture that is already one channel is returned unchanged, so this is safe
    to apply to anything and the mono case costs one comparison.
    """
    if channels <= 1:
        return pcm
    samples = _samples(pcm)
    usable = len(samples) - (len(samples) % channels)
    if usable <= 0:
        return b''
    one = samples[min(channel, channels - 1):usable:channels]
    if BIG_ENDIAN:
        one = array.array('h', one)
        one.byteswap()
    return one.tobytes()


def voice_channel_rms(pcm: bytes, channels: int = CAPTURE_CHANNELS,
                      channel: int = VOICE_CHANNEL) -> float:
    """Loudness of the channel the recording will actually keep, for the meter.

    The same channel ``voice_channel`` selects, deliberately. An earlier version
    took the loudest of the two so the bar would be responsive, and the result
    was a meter driven by the noisy right-hand microphone: in a silent room it
    read half full, which tells the person holding the device that their voice is
    arriving when nothing is. The bar has to measure the audio that will be
    recognised or it is not answering the question it is on screen to answer.
    """
    if channels <= 1:
        return rms(pcm)
    samples = _samples(pcm)
    usable = len(samples) - (len(samples) % channels)
    if usable <= 0:
        return 0.0
    one = samples[min(channel, channels - 1):usable:channels]
    step = max(1, len(one) // 512)
    taken = one[::step]
    return min(1.0, (sum(s * s for s in taken) / len(taken)) ** 0.5 / 32768.0)


def levels(pcm: bytes) -> tuple[float, float]:
    """(peak, rms) of a recording, both 0.0 to 1.0.

    The peak is exact, because it is the limit that decides how much gain can be
    applied without clipping and one clipped sample is audible. The loudness is
    estimated from at most ``RMS_SAMPLES`` evenly spaced samples, so measuring a
    two-minute recording costs what measuring a two-second one costs.
    """
    samples = _samples(pcm)
    if not samples:
        return 0.0, 0.0
    peak = max(max(samples), -min(samples)) / 32768.0
    step = max(1, len(samples) // RMS_SAMPLES)
    taken = samples[::step]
    total = sum(s * s for s in taken)
    return peak, (total / len(taken)) ** 0.5 / 32768.0


def recognition_gain(peak: float, rms: float) -> float:
    """How much to scale a recording by before asking for a transcript.

    Loudness is aimed at ``TARGET_RMS`` rather than the peak, so that one click
    in an otherwise quiet recording cannot decide the level for the speech. The
    peak is still a hard ceiling, which is what makes this unable to clip. A
    recording that is already loud enough is left alone: the gain is never below
    1.0, so nothing that works today is made quieter.
    """
    if peak <= SILENCE_PEAK or rms <= 0.0:
        return 1.0                      # the room, not a voice
    wanted = TARGET_RMS / rms
    without_clipping = CEILING_PEAK / peak
    return max(1.0, min(wanted, without_clipping, MAX_GAIN))


def too_quiet(peak: float) -> bool:
    """Was there a voice in this at all.

    Worth asking separately, because "nothing was recognised" and "the
    microphone heard nothing" look the same on screen and have different
    answers: one is a sentence the models could not make out, the other is
    standing too far away.
    """
    return peak <= SILENCE_PEAK


# What the interfaces say when no_signal() is true. Kept here so both of them
# say the same thing, and so the sentence sits next to the measurement that
# justifies it. It names the device rather than the person: nothing the person
# does with the microphone changes a line that is not being driven.
NO_MICROPHONE = '麦克风没有信号：设备没有在送出音频，重启设备后再试'

# The widest a recording may span and still be certain to contain no microphone
# at all. A live capture carries the room even when nobody speaks: measured on
# this device with the PGA at 36 dB, a silent room has an rms near 0.0072, which
# is about 236 counts of a 16-bit scale. A capture that never moves by more than
# a couple of counts from beginning to end is therefore not a quiet room, it is
# a data line nobody is driving.
DEAD_SPAN = 2


def no_signal(pcm: bytes) -> bool:
    """True when the capture carries no microphone data at all.

    Measured on typixdeck on 2026-09-14: ``arecord`` on the ESP32's USB audio
    card returned five seconds in which every single one of 240000 samples was
    exactly -1, on both channels, with the speaker playing a tone into the room
    at the same time. Every sample identical is not something a microphone
    produces; it is what the ESP32's I2S input reads when the ES8389 has stopped
    driving its data pin and GPIO48's pull-up holds the line high - 0xFFFF, which
    is -1 as a signed sample. A stream of exact zeros, which is what the firmware
    substitutes when it cannot read the codec at all, is caught by the same test.

    This matters on screen rather than only in a log. Audio like this reaches
    Moonshine, which answers it with an empty transcript and HTTP 200, and the
    interfaces then reported the one thing that is certainly not true: that the
    microphone heard something faint and the person should move closer. Asking
    this question first turns a misleading instruction into the real fault, and
    saves the several seconds of recognition that cannot succeed.
    """
    samples = _samples(pcm)
    if not samples:
        return True
    return max(samples) - min(samples) <= DEAD_SPAN


def meter(rms_value: float) -> float:
    """The level bar, 0.0 to 1.0, drawn against what the recogniser needs.

    A full bar means "loud enough to be recognised" and an empty bar means "the
    room, and nothing else". Both ends had to be set deliberately:

    The top is ``TARGET_RMS``. Drawn straight from the raw loudness the bar never
    left its left-hand end, because speech on this device measures an rms of
    about 0.028 on a scale to 1.0, and a working microphone looked exactly like a
    dead one.

    The bottom is ``NOISE_RMS``, and without it the bar was worse than useless.
    The room alone measures an rms near 0.0074, which against TARGET_RMS drew a
    bar a third full in complete silence - so the bar read "your voice is
    arriving" when the honest answer was that nobody had said anything, and there
    was no way to tell that from a voice the device could actually hear.

    The square root is kept because speech spends most of its time near the quiet
    end of its own range.
    """
    if rms_value <= NOISE_RMS:
        return 0.0
    return min(1.0, ((rms_value - NOISE_RMS) / (TARGET_RMS - NOISE_RMS)) ** 0.5)


def for_recognition(pcm: bytes) -> bytes:
    """The float32 buffer ``/api/stt`` wants, at a level it can hear.

    See the level constants above for the measurements behind this. Recordings
    from this device's microphone are an order of magnitude quieter than what
    the recogniser was trained on, and it answers audio that is too quiet with
    an empty transcript.
    """
    samples = _samples(pcm)
    if not samples:
        return b''
    peak = max(max(samples), -min(samples)) / 32768.0
    step = max(1, len(samples) // RMS_SAMPLES)
    taken = samples[::step]
    rms_value = (sum(s * s for s in taken) / len(taken)) ** 0.5 / 32768.0
    return _to_float32(samples, recognition_gain(peak, rms_value))


def rms(pcm: bytes) -> float:
    """Loudness of a block, 0.0 to 1.0, for the level meter.

    Sampled rather than summed: the meter is drawn at about 15 frames a second
    and does not need every sample, and stepping over the block keeps the cost
    flat no matter how much arrived at once.
    """
    samples = array.array('h')
    samples.frombytes(pcm[:len(pcm) - (len(pcm) % 2)])
    if not samples:
        return 0.0
    step = max(1, len(samples) // 512)
    taken = samples[::step]
    total = sum(s * s for s in taken)
    return min(1.0, (total / len(taken)) ** 0.5 / 32768.0)


def capture_command(arecord: str, device: str, rate: int,
                    channels: int = CAPTURE_CHANNELS) -> list[str]:
    """The exact recording command. A list, never a string.

    Kept as its own function so the argument vector can be tested without a
    sound card, and so there is one place where the format is decided rather
    than one place per caller.

    Two channels, not one: this card captures only in stereo and answers a mono
    request by averaging two microphones of very different quality. ``Recorder``
    takes the good one afterwards with ``voice_channel``.
    """
    return [arecord, '-q', '-D', device, '-f', 'S16_LE',
            '-r', str(rate), '-c', str(channels), '-t', 'raw']


def playback_command(aplay: str, device: str) -> list[str]:
    return [aplay, '-q', '-D', device, '-']


def complaint(stderr: str) -> str | None:
    """The first line of arecord's output that reports a real problem.

    Stopping a recording means sending SIGTERM to a process that is blocked in
    ``read``, and ALSA's tools say so::

        arecord: pcm_read:2272: read error: Interrupted system call

    That line is the sound of the stop working. Treating it as a failure threw
    away every recording this device ever made: the interface saw an error,
    reported it, and never sent the audio to be recognised - so speech
    recognition appeared to be broken while the microphone, the models and the
    backend were all fine.

    ``EINTR`` is by definition "a signal arrived", and the signal was ours.
    Anything else arecord has to say is still worth showing, including a second
    line after a benign first one, which is why this scans rather than taking
    ``splitlines()[0]``.
    """
    benign = (
        'Interrupted system call',   # we sent SIGTERM; this is the reply
        'Recording',                 # the banner some versions print despite -q
        'Terminated',
    )
    for line in stderr.splitlines():
        line = line.strip()
        if line and not any(mark in line for mark in benign):
            return line[:120]
    return None


class Recorder:
    """A recording that runs until it is stopped.

    There is no key-release event on this link (see ``tui.py``), so recording
    is press-to-start, press-to-stop. The captured audio is held in memory and
    handed over as one buffer; nothing is written to the eMMC, which on this
    device is the same card the operating system boots from.
    """

    def __init__(self, device: str = DEFAULT_DEVICE, rate: int = SAMPLE_RATE):
        self.device, self.rate = device, rate
        self._process: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._blocks: list[bytes] = []
        self._lock = threading.Lock()
        self._level = 0.0
        self._started = 0.0
        self._error: str | None = None

    # -- lifetime ------------------------------------------------------------
    def start(self) -> None:
        if self._process is not None:
            return
        arecord = _tool('arecord')
        try:
            self._process = subprocess.Popen(
                capture_command(arecord, self.device, self.rate),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
        except OSError as exc:
            raise AudioUnavailable(f'could not start arecord: {exc}') from exc
        self._blocks, self._level, self._error = [], 0.0, None
        self._started = time.monotonic()
        self._thread = threading.Thread(target=self._drain, daemon=True)
        self._thread.start()

    def _drain(self) -> None:
        stream = self._process.stdout
        limit = int(MAX_SECONDS * self.rate) * SAMPLE_BYTES * CAPTURE_CHANNELS
        held = 0
        while True:
            try:
                block = stream.read(READ_BLOCK)
            except (OSError, ValueError):
                break
            if not block:
                break
            with self._lock:
                self._blocks.append(block)
                held += len(block)
                self._level = voice_channel_rms(block)
            if held >= limit:
                # Stop the capture but keep what was recorded: a two-minute
                # take that ends by itself is better than one that is discarded.
                self._terminate()
                break

    def _terminate(self) -> None:
        process, self._process = self._process, None
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        try:
            error = process.stderr.read().decode('utf-8', 'replace').strip()
        except (OSError, ValueError, AttributeError):
            error = ''
        # Everything that reaches here was asked to stop, so the noise a
        # terminated arecord makes is expected. complaint() knows the
        # difference between that and a card that went away.
        self._error = complaint(error)
        for stream in (process.stdout, process.stderr):
            try:
                stream.close()
            except (OSError, ValueError, AttributeError):
                pass

    def stop(self) -> bytes:
        """End the recording and return one microphone's raw 16-bit PCM.

        The capture is stereo because the card offers nothing else; what comes
        back here is single-channel, so every caller downstream - the level
        measurements, the duration, ``for_recognition`` - works on one voice
        rather than on a good microphone averaged with a noisy one.
        """
        self._terminate()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=2)
        with self._lock:
            return voice_channel(b''.join(self._blocks))

    def cancel(self) -> None:
        self.stop()
        with self._lock:
            self._blocks = []

    # -- what the interface asks while it runs -------------------------------
    @property
    def running(self) -> bool:
        return self._process is not None

    @property
    def level(self) -> float:
        with self._lock:
            return self._level

    @property
    def seconds(self) -> float:
        if not self._started:
            return 0.0
        with self._lock:
            held = sum(len(b) for b in self._blocks)
        return held / (self.rate * SAMPLE_BYTES * CAPTURE_CHANNELS)

    @property
    def error(self) -> str | None:
        return self._error


class Player:
    """Plays a WAV buffer, and can be interrupted.

    Speech synthesis of a long paragraph produces a long clip, and the person
    holding the device has to be able to cut it off and say the next thing.
    """

    def __init__(self, device: str = DEFAULT_DEVICE):
        self.device = device
        self._process: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._error: str | None = None

    def play(self, wav: bytes) -> None:
        """Start playing. Returns immediately; the interface keeps drawing."""
        self.stop()
        if not wav:
            return
        aplay = _tool('aplay')
        try:
            self._process = subprocess.Popen(
                playback_command(aplay, self.device),
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE)
        except OSError as exc:
            raise AudioUnavailable(f'could not start aplay: {exc}') from exc
        self._error = None
        self._thread = threading.Thread(target=self._feed, args=(wav,), daemon=True)
        self._thread.start()

    def _feed(self, wav: bytes) -> None:
        process = self._process
        if process is None:
            return
        try:
            process.stdin.write(wav)
            process.stdin.close()
        except (BrokenPipeError, OSError, ValueError):
            # Interrupted playback closes the pipe under us; that is the point.
            return
        try:
            process.wait(timeout=MAX_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()

    @property
    def playing(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def stop(self) -> None:
        process, self._process = self._process, None
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
        for stream in (process.stdin, process.stderr):
            try:
                if stream:
                    stream.close()
            except (OSError, ValueError):
                pass
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=1)
