# Audio hardware notes

Three places in the firmware point here for the reasoning behind a constant
that looks arbitrary:

- `firmware/esp32s3/main/audio.c` for `no_dac_ref` and for the DAC channel swap
- `firmware/esp32s3/main/board_pins.h` for `BOARD1_ADC2_DEAD_WORKAROUND`

Each of those is a workaround for something measured on real boards. Written
down here rather than in the source, because the evidence is longer than the
code it justifies, and because several of the findings only make sense next to
each other.

## The signal path

```
MIC3 (left)  ─ ZTS6056 ─→ ES8389 MIC1 (pin 24/23, pseudo-differential) ─→ ADC left
MIC4 (right) ─ ZTS6056 ─→ ES8389 MIC2 (pin 22/21, pseudo-differential) ─→ ADC right
                                        │
                                   I2S DIN = GPIO48
                                        ↓
                              ESP32-S3 ─ USB UAC ─→ CM5 ─→ arecord
```

Playback runs the other way through `I2S DOUT = GPIO47`. Both directions share
one I2S port in full duplex, so they share the clock: 48 kHz, 16-bit, stereo.
`AUDIO_3V3` is switched by AW9523 `P1_0` (`DAC_3V3_EN`), which `main.c` drives
high at start-up; nothing else in the microphone path needs an IO operation.

The recogniser wants 16 kHz mono, so `linux/apps/audio.py` asks ALSA for that
and ALSA resamples. What it does *not* do is pick a microphone — see below.

## GPIO47 and GPIO48 are shared with the LCD

`R50`/`R51` (0 Ω) tie the I2S data pins to `ESP_LCD_SCLK`/`ESP_LCD_MOSI`. The
JD9168S panel needs a one-time SPI initialisation sequence at boot, so the
ordering is fixed and `main.c` guarantees it:

1. `lcd_jd9168s_spi_init()` drives GPIO47/48 as SPI2 and sends the panel's
   init sequence, then calls `spi_bus_free(SPI2_HOST)`.
2. `audio_start()` reconfigures the same pins as I2S DOUT/DIN.

After step 1 the panel runs on the RGB parallel interface (GPIO 1–4, 8–18,
38–42) and never touches 47/48 again, so the two uses do not overlap in time.

One side effect of step 1 matters for diagnosis: `spi_bus_free()` resets the
pins through `gpio_reset_pin()`, which **enables their pull-ups**. GPIO48
therefore idles high. That is why a codec which has stopped driving the line
reads as `0xFFFF` — `-1` as a signed sample — rather than as zeros.

## The ES8389's I2C address has to be probed

`AD1` is left floating, so the 7-bit address settles anywhere in `0x10`–`0x13`.
`audio_start()` probes the candidates in order and uses the first that answers.
A board where none answer has no `DAC_3V3`; that is a power fault, not an
address problem, and the log says so.

The old `ES8389_I2C_ADDR 0x20` constant contradicted every measurement and was
never referenced by anything. It is gone.

## `no_dac_ref` must be true

With `no_dac_ref = false` the driver enables the codec's AEC reference mode, in
which the ADC's right slot is replaced by a loopback of the DAC output. MIC2's
signal then never reaches I2S at all. Recording appears to work — there is data
on the line — and the right channel is an echo of whatever is playing.

## The two microphones are not equally useful

The card offers capture only as stereo, so asking ALSA for one channel averages
both rather than selecting one. Measured on typixdeck on 2026-09-14, three
takes of room noise and three of the same sentence through the deck's own
speaker, with the PGA at 24.5 dB:

| channel | noise rms | signal rms | signal-to-noise |
| --- | --- | --- | --- |
| FL | 0.00200 | 0.00518 | +8.2 dB |
| FR | 0.00511 | 0.00464 | −0.8 dB |

The right-hand microphone hears its own noise about as loudly as it hears a
voice. Averaging the two drags a usable +8 dB channel down to roughly −1 dB,
and the transcripts show it — the same takes, each channel through the same
level correction:

```
FL        今天天气很好，我们一起去公园      (and two near misses)
FR        fragments and invented syllables
averaged  nothing at all
```

So `linux/apps/audio.py` selects one channel and discards the other. This is
worth about 9 dB over what ALSA's average produced, and it costs nothing.
`MIXOS_VOICE_CHANNEL` exists because a quiet right channel may be particular to
this unit, so a differently behaved board is a deployment setting rather than a
code change.

**The louder channel is not the better one.** FR reads higher on a meter
precisely because its noise floor is higher, which is the trap this table
exists to document: anyone re-measuring with a level meter and no speech will
conclude FR is the stronger microphone and be wrong.

## Board #1 has a dead right-hand analogue front end

Board #1 (ESP MAC `70:04:1D:D7:E3:40`) produces nothing at all on ADC2 — a
separate and more severe fault than the noise figures above.
`BOARD1_ADC2_DEAD_WORKAROUND` sets REG0x23 bit4, which makes the right channel
a digital copy of the left, so a stereo capture at least carries the signal in
both slots.

Board #2 (`70:04:1D:D8:52:70`) and any healthy board must leave it at `0`, or
true stereo is thrown away. **The constant is currently `0`.** Check the MAC
before changing it.

## The speakers are wired left-to-right

REG0x44 (`DAC MIX CONTROL`) bit5 routes DAC2→DAC1 and bit4 routes DAC1→DAC2;
setting both (`0x30`) is a complete digital L/R swap. `audio_set_dac_lr_swap()`
applies it when playing through the board's own speakers and removes it when
headphones are inserted, because only the speaker path is reversed.

It is a read-modify-write under the codec mutex. It has to be: a concurrent
volume change used to be able to land between the read and the write, after
which this function would restore the stale register.

## Microphone PGA gain: 36 dB, not 24

The ES8389 goes to 36.5 dB (`ES8389_MIC_GAIN_36_5DB`) and the driver maps a
request to the nearest step, so anything ≥ 36 lands on the top one.

This was 24.0 dB for a long time, on the theory that holding the device against
its own speaker would clip. Re-measured against real speech on 2026-09-14 the
conclusion reversed: voice peaked at only 0.06–0.08 of full scale, wasting
about 22 dB of headroom, and Moonshine answers audio that is too quiet with an
empty transcript and HTTP 200 rather than an error. On screen an empty
transcript is indistinguishable from broken speech recognition, which is
exactly how it was reported.

At 36.5 dB peaks land near 0.25–0.33, still far from clipping, and the
signal-to-noise improves by 12 dB. This is analogue gain ahead of the ADC, so
it improves what the software normalisation in `for_recognition()` cannot.

## 2026-09-14: the codec stops driving I2S after many hours

**Symptom.** Both interfaces reported `没听到声音，离麦克风近一点再说`.
Speaking louder or standing closer changed nothing.

**Measurement.** `tools/probe_deck_mic.py`, four seconds of capture with the
device up for 19 hours:

```
FL  peak 0.0000  rms 0.00003  span 0 counts  min -1  max -1
FR  peak 0.0000  rms 0.00003  span 0 counts  min -1  max -1
```

Every one of 128000 samples was exactly `-1`, on both channels. A live capture
carries the room even in silence — a quiet room spans some hundreds of counts —
so a span of zero is not a quiet room. It is GPIO48's pull-up holding a line
that nothing is driving.

**What it was not.** Playback still worked, the codec still answered on I2C,
and `audio_read()` returned success — it zero-fills on failure, and these were
not zeros, so I2S was reading fine. The fault was downstream of the ESP32 and
upstream of the pin: the ES8389 had stopped driving its ADC data output.

**Why nothing noticed.** `audio_ready()` answers "is there an open handle".
The handle stayed open the whole time, so the screen went on reporting the
audio path as ready, and the microphone was dead until the next reboot.

**Fix.** Reflashing — and therefore restarting the ESP32 — restored it
immediately:

```
FL  peak 0.0311  rms 0.00822  span 1994 counts
FR  peak 0.1124  rms 0.02488  span 6957 counts
```

Those levels also confirm the 36.5 dB gain took effect: FL's noise floor of
0.0075–0.0082 is very close to 4× the 0.00200 measured at 24.5 dB, which is the
+12 dB the gain change was worth. The 2.8:1 ratio between FR and FL noise
matches the 2.6:1 in the table above, i.e. the two microphones still differ the
way they always did.

**Why it needed a code change.** A fault that takes nineteen hours to appear
and a reboot to clear will happen again, and the device could not see it. So
`audio.c` now watches for it:

- `audio_note_capture()` tests every buffer the USB host records against the
  same two-count dead-line threshold `linux/apps/audio.py` uses, so the device
  and the interfaces cannot disagree about what a dead microphone is.
- `audio_poll_capture()` takes a short capture of its own once a second **when
  nothing is recording**, so a codec that dies while the device sits idle is
  found before someone tries to use it, rather than by the recording that the
  fault would ruin.
- Two unbroken seconds of dead line triggers `audio_recover()`, which
  unpublishes the handle, closes the codec, tears down I2S and repeats the
  start-up sequence exactly. `audio_recovery_count()` reports how often that
  happened, because a codec rebuilt repeatedly is a different fault from one
  rebuilt never, and a working repair hides the difference.

Recovering rather than rebooting keeps the terminal, the keyboard and the
screen alive, none of which were affected by the fault.

**Watch out for.** `audio_recover()` runs on the device task while the USB task
may be mid-capture, so the handle is cleared under the mutex and closed only
after that critical section ends. `audio_read()` and `audio_write()` re-read
the handle *inside* the lock for the same reason; they used to check it outside
and pass it in, which was safe only while nothing ever closed the codec.
