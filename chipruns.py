#!/usr/bin/env python3
"""Render chromatic runs through Furnace's chip emulators.

For every chip Furnace emulates, this writes a .fur project that plays a
chromatic run (A3 = 220 Hz up to A4 = 440 Hz by default) with rests between
notes, renders it with Furnace's own exporter, and saves a WAV labelled with
the chip. With the `presets` target it does the same for every instrument
preset Furnace ships (/usr/share/furnace/instruments), each on its native chip.

Chip runs use no instrument, so each chip plays Furnace's built-in default
voice: what you hear when you type notes into a blank song. Sample-only chips
get a looped saw sample at index 0.

Pitch: on some chips Furnace sounds an octave or two away from its note names
(e.g. NES pulse and PC Engine one octave up, SN76489 two; the same in stable
0.6.8.1). Before rendering, each chip channel is probed and the written notes
are transposed so the audio really spans the requested pitches. Every render is
measured afterwards; the manifest keeps written note, target and measured Hz.

Needs bin/furnace-batch (see ./build-furnace.sh). Stock Furnace can render a
.fur from the command line, but only the patched build can import presets
through Furnace's loaders and dump its chip table. Pitch probing needs numpy.

Outputs (under --out):
  chips/<chip>.fur, .wav                      one run per chip
  chips/<chip>/<chip>__chNN-<channel>.wav     with --channels types|all
  presets/<chip>/<preset>.fur, .wav           one run per preset instrument
  manifest.json                               every file with chip, preset, notes and measured pitch

Each WAV carries a LIST/INFO title (chip and preset) and a cue point named
after the sounding note at every note onset.
"""

import argparse
import array
import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import wave
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import numpy as np
except ImportError:
    np = None

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_FURNACE = os.path.join(HERE, "bin", "furnace-batch")
PRESET_DIR = "/usr/share/furnace/instruments"
USER_CONFIG = os.path.expanduser("~/.config/furnace/furnace.cfg")

# extensions DivEngine::instrumentFromFile understands
INS_EXTS = {"fui", "dmp", "tfi", "vgi", "eif", "s3i", "sbi", "opli", "opni", "y12", "bnk", "ff", "gyb", "opm", "wopl", "wopn"}

NOTE_NAMES = ["C-", "C#", "D-", "D#", "E-", "F-", "F#", "G-", "G#", "A-", "A#", "B-"]
NOTE_A4 = 117  # pattern note values start at C-(-5) = 0; 117 is A-4
NOTE_OFF = 180
NOTE_RELEASE = 181  # "===": key off + macro release

# DivInstrumentType, in enum order (51 was YMF292, never shipped)
INS_TYPES = [
    "STD", "FM", "GB", "C64", "AMIGA", "PCE", "AY", "AY8930", "TIA", "SAA1099", "VIC", "PET", "VRC6",
    "OPLL", "OPL", "FDS", "VBOY", "N163", "SCC", "OPZ", "POKEY", "BEEPER", "SWAN", "MIKEY", "VERA",
    "X1_010", "VRC6_SAW", "ES5506", "MULTIPCM", "SNES", "SU", "NAMCO", "OPL_DRUMS", "OPM", "NES",
    "MSM6258", "MSM6295", "ADPCMA", "ADPCMB", "SEGAPCM", "QSOUND", "YMZ280B", "RF5C68", "MSM5232",
    "T6W28", "K007232", "GA20", "POKEMINI", "SM8521", "PV1000", "K053260", "YMF292", "TED", "C140",
    "C219", "ESFM", "POWERNOISE", "POWERNOISE_SLOPE", "DAVE", "NDS", "GBA_DMA", "GBA_MINMOD",
    "BIFURCATOR", "SID2", "SUPERVISION", "UPD1771C", "SID3",
]

# preset instrument type -> chip (file ID) it is rendered on.
# Types not listed go to the first chip with a channel that prefers that type.
PRESET_CHIP = {
    0: 0x03,   # STD -> TI SN76489
    1: 0x83,   # FM (OPN) -> YM2612
    2: 0x04,   # Game Boy
    3: 0x47,   # C64 -> SID 6581
    6: 0x80,   # AY-3-8910
    7: 0x9a,   # AY8930
    8: 0x84,   # TIA
    9: 0x97,   # SAA1099
    10: 0x85,  # VIC-20
    13: 0x89,  # OPLL -> YM2413
    14: 0x91,  # OPL -> YMF262 (OPL3: plays 2-op and 4-op patches)
    15: 0x8a,  # FDS
    19: 0x98,  # OPZ -> YM2414
    23: 0xa8,  # MIKEY -> Atari Lynx
    24: 0xac,  # VERA
    31: 0xb9,  # NAMCO -> Namco WSG
    32: 0xa4,  # OPL_DRUMS -> OPL3 with drums
    33: 0x82,  # OPM -> YM2151
    34: 0x06,  # NES
    55: 0xd1,  # ESFM
}

# chips that build their sample table from the song's instruments: a chip run
# without an instrument is silent, so give them a blank one of the chip's type
NEEDS_INSTRUMENT = {0x92}  # MultiPCM

# chip definitions that share silicon with a base chip; channel 1 sounds the same
VARIANT_RE = re.compile(r"Extended Channel|CSM|DualPCM|with drums|with software PCM")

SAW_PERIOD = 64  # samples per cycle of the saw used by sample-only chips
C4_HZ = 440.0 * 2 ** (-9 / 12)


def parse_note(text):
    """Note name (A3, A-3, A#3, Bb3) -> pattern value. Octave numbers are Furnace's, A4/A-4 = 440 Hz."""
    m = re.fullmatch(r"([A-Ga-g])([-#b]?)(-?\d+)", text.strip())
    if not m:
        raise argparse.ArgumentTypeError(f"bad note {text!r} (use e.g. A3, A#3, Bb3)")
    letter, acc, octave = m.group(1).upper(), m.group(2), int(m.group(3))
    semi = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}[letter]
    semi += {"#": 1, "b": -1}.get(acc, 0)
    value = (octave + 5) * 12 + semi
    if not 0 <= value < 180:
        raise argparse.ArgumentTypeError(f"note {text!r} is out of Furnace's range")
    return value


def note_name(value):
    """Furnace pattern spelling, e.g. A-3."""
    return f"{NOTE_NAMES[value % 12]}{value // 12 - 5}"


def pitch_name(value):
    """Scientific pitch spelling, e.g. A3 (A4 = 440 Hz)."""
    return f"{NOTE_NAMES[value % 12].rstrip('-')}{value // 12 - 5}"


def note_hz(value, tuning=440.0):
    return tuning * 2 ** ((value - NOTE_A4) / 12)


def slug(text):
    text = text.replace("#", "s").replace("+", "plus")
    return re.sub(r"[^A-Za-z0-9.]+", "-", text).strip("-.") or "untitled"


# --------------------------------------------------------------------------
# timing


class Timing:
    """Row layout of a run: note, rest, note, rest... then a tail and FF00 (stop)."""

    def __init__(self, tick_rate, speed, note_sec, rest_sec, tail_sec, note_end):
        self.tick_rate, self.speed, self.note_end = tick_rate, speed, note_end
        self.rows_per_sec = tick_rate / speed
        self.note_rows = max(1, round(note_sec * self.rows_per_sec))
        self.rest_rows = max(1, round(rest_sec * self.rows_per_sec))
        self.tail_rows = max(1, round(tail_sec * self.rows_per_sec))
        self.step = self.note_rows + self.rest_rows

    def total_rows(self, count):
        return count * self.step + self.tail_rows

    def seconds(self, count):
        return self.total_rows(count) / self.rows_per_sec

    def events(self, written):
        out = []
        for i, note in enumerate(written):
            row = i * self.step
            out.append((row, "on", note))
            if self.note_end == "release" and self.rest_rows >= 2:
                out.append((row + self.note_rows, "release", None))
                out.append((row + self.step - 1, "off", None))
            else:
                out.append((row + self.note_rows, "off", None))
        return out

    def schedule(self, sounding, transpose):
        return [{
            "note": pitch_name(n),
            "hz": round(note_hz(n), 3),
            "written": note_name(n + transpose),
            "onset_sec": round(i * self.step / self.rows_per_sec, 6),
            "dur_sec": round(self.note_rows / self.rows_per_sec, 6),
        } for i, n in enumerate(sounding)]

    def describe(self, sounding):
        return (
            f"Chromatic run {pitch_name(sounding[0])} ({note_hz(sounding[0]):g} Hz) to {pitch_name(sounding[-1])} "
            f"({note_hz(sounding[-1]):g} Hz), {len(sounding)} notes. Each note {self.note_rows / self.rows_per_sec:g} s, "
            f"then {self.rest_rows / self.rows_per_sec:g} s rest (note end: {self.note_end}). "
            f"{self.tick_rate:g} Hz ticks, speed {self.speed}. Generated by furnace-chipruns."
        )


# --------------------------------------------------------------------------
# .fur writer (mirrors DivEngine::saveFur, INF2 format)


class Writer:
    def __init__(self):
        self.b = bytearray()

    def tell(self):
        return len(self.b)

    def u8(self, v):
        self.b += struct.pack("<B", v)

    def u16(self, v):
        self.b += struct.pack("<H", v)

    def i32(self, v):
        self.b += struct.pack("<i", v)

    def u32(self, v):
        self.b += struct.pack("<I", v)

    def f32(self, v):
        self.b += struct.pack("<f", v)

    def str(self, s):
        self.b += s.encode("utf-8") + b"\0"

    def raw(self, data):
        self.b += data

    def put_u32(self, pos, v):
        self.b[pos:pos + 4] = struct.pack("<I", v)

    def block(self, magic):
        """Start a block; returns a closer that fills in its size."""
        self.raw(magic)
        size_pos = self.tell()
        self.u32(0)
        return lambda: self.put_u32(size_pos, self.tell() - size_pos - 4)


def encode_pattern(rows):
    """rows: one dict per row with any of note/ins/vol/fx/fxval, or None if empty."""
    out = bytearray()
    empty = 0
    for cell in rows:
        if not cell:
            empty += 1
            if empty > 127:
                out.append(128 | (empty - 2))
                empty = 0
            continue
        if empty > 1:
            out.append(128 | (empty - 2))
        elif empty:
            out.append(0)
        empty = 0
        mask = 0
        for bit, key in ((1, "note"), (2, "ins"), (4, "vol"), (8, "fx"), (16, "fxval")):
            if key in cell:
                mask |= bit
        out.append(mask)
        for key in ("note", "ins", "vol", "fx", "fxval"):
            if key in cell:
                out.append(cell[key])
    out.append(0xFF)
    return bytes(out)


def saw_sample(note_sec, high_hz):
    """Looped 16-bit saw, long enough to cover a whole note on chips that can't loop."""
    rate = SAW_PERIOD * C4_HZ
    needed = note_sec * rate * high_hz / C4_HZ * 1.1
    cycles = max(64, int(needed / SAW_PERIOD) + 1)
    cycle = array.array("h", (int(-24000 + 48000 * i / SAW_PERIOD) for i in range(SAW_PERIOD)))
    data = cycle * cycles
    if sys.byteorder != "little":
        data.byteswap()
    return {"name": "saw", "rate": round(rate), "data": data.tobytes(), "length": len(cycle) * cycles}


def build_fur(version, chip, timing, voices, song_name, comment, ins=None, sample=None, compat=None, blank_ins=None):
    """Return .fur bytes for `chip`; voices maps channel -> written notes (all the same length).

    ins: instrument number to put on every note. blank_ins: instrument type of a
    default instrument to store as instrument 0 (implies ins=0).
    """
    if blank_ins is not None:
        ins = 0
    chans = chip["channels"]
    pat_len = 64
    count = len(next(iter(voices.values())))
    total_rows = timing.total_rows(count)
    orders = -(-total_rows // pat_len)
    if orders > 256:
        raise ValueError("run too long for one song (more than 256 orders of 64 rows)")

    runs = {}
    for c, written in voices.items():
        run = [None] * (orders * pat_len)
        for row, kind, note in timing.events(written):
            if kind == "on":
                run[row] = {"note": note} if ins is None else {"note": note, "ins": ins}
            else:
                run[row] = {"note": NOTE_OFF if kind == "off" else NOTE_RELEASE}
        runs[c] = run

    w = Writer()
    w.raw(b"-Furnace module-")
    w.u16(version)
    w.u16(0)
    w.u32(32)
    w.u32(0)
    w.u32(0)

    # song info
    close = w.block(b"INF2")
    for s in (song_name, "furnace-chipruns", chip["name"], "chromatic runs", "", "", "", ""):
        w.str(s)
    w.f32(440.0)  # A-4 tuning
    w.u8(1)  # automatic system name
    w.f32(1.0)  # master volume
    w.u16(chans)
    w.u16(1)  # one chip
    w.u16(chip["id"])
    w.u16(chans)
    w.f32(1.0)  # chip volume
    w.f32(0.0)  # panning
    w.f32(0.0)  # front/rear
    w.u32(0)  # patchbay connections
    w.u8(1)  # automatic patchbay

    def element(kind, n):
        w.u8(kind)
        w.u32(n)
        pos = w.tell()
        for _ in range(n):
            w.u32(0)
        return pos

    sng_ptr = element(0x01, 1)
    element(0x02, 1)  # chip flags: pointer left 0, so Furnace uses the chip's defaults
    adir_ptr = element(0x03, 3)
    ins_ptr = element(0x04, 1) if blank_ins is not None else None
    smp_ptr = element(0x06, 1) if sample else None
    pat_ptr = element(0x07, chans * orders)
    cflg_ptr = element(0x08, 1) if compat else None
    cmnt_ptr = element(0x09, 1)
    w.u8(0)
    close()

    # sub-song
    w.put_u32(sng_ptr, w.tell())
    close = w.block(b"SNG2")
    w.f32(timing.tick_rate)
    w.u8(1)  # arpeggio speed
    w.u8(0)  # effect speed divider
    w.u16(pat_len)
    w.u16(orders)
    w.u8(4)
    w.u8(16)
    w.u16(150)  # virtual tempo
    w.u16(150)
    w.u8(1)  # speed pattern length
    w.u16(timing.speed)
    for _ in range(15):
        w.u16(0)
    w.str("")
    w.str("")
    for _ in range(chans):
        for o in range(orders):
            w.u8(o)
    for _ in range(chans):
        w.u8(1)  # effect columns
    for _ in range(chans):
        w.u8(3)  # shown in pattern view and channel oscilloscope
    for _ in range(chans):
        w.u8(0)  # not collapsed
    for _ in range(chans * 2):
        w.str("")  # default channel names
    for _ in range(chans):
        w.u32(0)  # default channel colours
    close()

    if compat:
        w.put_u32(cflg_ptr, w.tell())
        close = w.block(b"CFLG")
        w.str("".join(f"{k}={v}\n" for k, v in compat.items()))
        close()

    # song comment
    w.put_u32(cmnt_ptr, w.tell())
    close = w.block(b"CMNT")
    w.str(comment)
    close()

    # asset directories (none)
    for i in range(3):
        w.put_u32(adir_ptr + 4 * i, w.tell())
        close = w.block(b"ADIR")
        w.u32(0)
        close()

    if blank_ins is not None:
        w.put_u32(ins_ptr, w.tell())
        close = w.block(b"INS2")
        w.u16(version)
        w.u16(blank_ins)
        name = b"Instrument 0\0"
        w.raw(b"NA" + struct.pack("<H", len(name)) + name)
        w.raw(b"EN")
        close()

    if sample:
        w.put_u32(smp_ptr, w.tell())
        close = w.block(b"SMP2")
        w.str(sample["name"])
        w.u32(sample["length"])
        w.u32(sample["rate"])  # compatibility rate
        w.u32(sample["rate"])  # C-4 rate
        w.u8(16)  # 16-bit PCM
        w.u8(0)  # loop forward
        w.u8(1)  # BRR emphasis (Furnace default)
        w.u8(0)
        w.i32(0)  # loop start
        w.i32(sample["length"])  # loop end
        for _ in range(4):
            w.u32(0xFFFFFFFF)  # present in every chip's memory
        w.raw(sample["data"])
        close()

    # patterns: channel-major, pattern index = order index
    n = 0
    for c in range(chans):
        for o in range(orders):
            rows = runs[c][o * pat_len:(o + 1) * pat_len] if c in runs else [None] * pat_len
            if c == 0 and o == orders - 1:
                rows = list(rows)
                last = (total_rows - 1) % pat_len
                rows[last] = dict(rows[last] or {}, fx=0xFF, fxval=0x00)  # FF00: stop song
            w.put_u32(pat_ptr + 4 * n, w.tell())
            n += 1
            close = w.block(b"PATN")
            w.u8(0)  # sub-song
            w.u8(c)
            w.u16(o)
            w.str("")
            w.raw(encode_pattern(rows))
            close()

    return bytes(w.b)


# --------------------------------------------------------------------------
# Furnace process wrapper


class Furnace:
    def __init__(self, exe, rate, workdir):
        if not os.access(exe, os.X_OK):
            raise SystemExit(f"{exe} not found; build it with ./build-furnace.sh")
        self.exe = exe
        self.tmp = workdir
        # private config: the user's settings (emulation cores) with our sample rate
        confdir = os.path.join(workdir, "config")
        os.makedirs(os.path.join(confdir, "furnace"), exist_ok=True)
        lines = []
        if os.path.exists(USER_CONFIG):
            with open(USER_CONFIG, encoding="utf-8", errors="replace") as f:
                lines = f.read().splitlines()
        if not lines or lines[0] != "!DIV_CONFIG_START!":
            lines = ["!DIV_CONFIG_START!", "~DIV_CONFIG_END~"]
        overrides = {"audioRate": str(rate), "audioEngine": "SDL", "saveUnusedPatterns": "0"}
        lines = [l for l in lines if l.split("=", 1)[0] not in overrides]
        lines[1:1] = [f"{k}={v}" for k, v in overrides.items()]
        with open(os.path.join(confdir, "furnace", "furnace.cfg"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        self.env = dict(os.environ, XDG_CONFIG_HOME=confdir)
        r = subprocess.run([exe, "-version"], env=self.env, capture_output=True, text=True, errors="replace")
        m = re.search(r"Furnace version (dev(\d+))", r.stdout + r.stderr)
        if not m:
            raise SystemExit(f"could not read a dev build number from {exe} -version")
        self.version_name, self.version = m.group(1), int(m.group(2))
        self.software = f"Furnace {self.version_name} via furnace-chipruns"

    def run(self, *args):
        r = subprocess.run(
            [self.exe, "-loglevel", "error", "-noreport", *args],
            env=self.env, capture_output=True, text=True, errors="replace",
        )
        if r.returncode != 0:
            msg = re.sub(r"\x1b\[[0-9;]*m", "", (r.stderr + r.stdout)).strip().splitlines()
            raise RuntimeError(" | ".join(msg[-3:]) or f"furnace exited with {r.returncode}")

    def json(self, *args):
        fd, path = tempfile.mkstemp(suffix=".json", dir=self.tmp)
        os.close(fd)
        try:
            self.run(*args, "-jsonout", path)
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        finally:
            os.unlink(path)

    def chips(self):
        return self.json("-sysjson")

    def instruments(self, path):
        return self.json("-insinfo", path)

    def temp(self, suffix):
        fd, path = tempfile.mkstemp(suffix=suffix, dir=self.tmp)
        os.close(fd)
        return path


# --------------------------------------------------------------------------
# pitch measurement (numpy)


def read_mono(path):
    with wave.open(path, "rb") as w:
        nch, rate, frames = w.getnchannels(), w.getframerate(), w.getnframes()
        x = np.frombuffer(w.readframes(frames), dtype="<i2").astype(np.float64)
    return x.reshape(-1, nch).mean(axis=1), rate


def yin(x, sr, fmin=40.0, fmax=3000.0, threshold=0.2):
    """YIN f0 of one frame; returns (hz, aperiodicity) or (None, 1.0)."""
    tmax = int(sr / fmin)
    win = int(sr * 0.04)
    if len(x) < win + tmax + 1:
        return None, 1.0
    frame = x[:win + tmax] - x[:win + tmax].mean()
    sq = np.concatenate(([0.0], np.cumsum(frame * frame)))
    n = 1 << (len(frame) + win).bit_length()
    acf = np.fft.irfft(np.fft.rfft(frame, n) * np.conj(np.fft.rfft(frame[:win], n)), n)[:tmax]
    taus = np.arange(tmax)
    d = sq[win] + (sq[taus + win] - sq[taus]) - 2 * acf
    d[0] = 0
    cmnd = np.ones(tmax)
    running = np.cumsum(d[1:])
    cmnd[1:] = d[1:] * np.arange(1, tmax) / np.maximum(running, 1e-9)
    lo = max(2, int(sr / fmax))
    below = np.nonzero(cmnd[lo:tmax - 1] < threshold)[0]
    if len(below):
        t = lo + below[0]
        while t + 1 < tmax - 1 and cmnd[t + 1] < cmnd[t]:
            t += 1
    else:
        t = lo + int(np.argmin(cmnd[lo:tmax - 1]))
    if cmnd[t] > 0.5:
        return None, float(cmnd[t])
    a, b, c = cmnd[t - 1], cmnd[t], cmnd[t + 1]
    den = a - 2 * b + c
    shift = 0.5 * (a - c) / den if den else 0.0
    return sr / (t + shift), float(cmnd[t])


def note_pitch(x, sr, onset, dur):
    """Median f0 over the loud frames of a note, or None if unpitched/silent.

    Only frames within 12 dB of the note's loudest frame count, so decaying
    voices (C64, Game Boy envelopes) are measured before they fade out.
    """
    a, b = int((onset + 0.15 * dur) * sr), int((onset + 0.95 * dur) * sr)
    seg = x[a:b]
    span = int(sr * 0.07)  # one YIN frame: 40 ms window + 25 ms of lags
    if len(seg) < span:
        return None
    starts = np.linspace(0, len(seg) - span, 8).astype(int)
    rms = np.array([np.sqrt(np.mean(seg[s:s + span] ** 2)) for s in starts])
    keep = rms >= max(30.0, rms.max() / 4)  # 30 is about -60 dBFS
    found = []
    for s in starts[keep]:
        hz, ap = yin(seg[s:], sr)
        if hz and ap < 0.25:
            found.append(hz)
    return float(np.median(found)) if found else None


def lowpass(x, sr, cutoff):
    """Zero-phase 8th-order Butterworth-shaped low-pass (FFT)."""
    n = 1 << (len(x) - 1).bit_length()
    f = np.fft.rfftfreq(n, 1 / sr)
    return np.fft.irfft(np.fft.rfft(x, n) / np.sqrt(1 + (f / cutoff) ** 16), n)[:len(x)]


def measure(path, schedule):
    """Fill measured_hz (YIN estimate) per note; returns median error in cents (None if unpitched).

    The audio is low-passed first: chip hash above the harmonics we need (e.g.
    Namco 163 channel multiplexing) otherwise breaks YIN's period dips.
    """
    x, sr = read_mono(path)
    x = lowpass(x, sr, max(2000.0, 6 * max(s["hz"] for s in schedule)))
    errors = []
    for s in schedule:
        hz = note_pitch(x, sr, s["onset_sec"], s["dur_sec"])
        s["measured_hz"] = round(hz, 2) if hz else None
        if hz:
            errors.append(1200 * math.log2(hz / s["hz"]))
    return round(float(np.median(errors)), 1) if len(errors) >= 3 else None


class Calibration:
    """Per chip channel: how far Furnace's written notes sound from their names, in octaves."""

    def __init__(self, fur, args, path):
        self.fur, self.args, self.path = fur, args, path
        self.lock = threading.Lock()
        self.data = {}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                self.data = json.load(f)

    def key(self, chip, chan):
        return f"{self.fur.version_name}|0x{chip['id']:02x}|{chan}"

    def get(self, chip, chan):
        with self.lock:
            return self.data.get(self.key(chip, chan))

    def probe(self, chip, chan):
        """Render A3, E4, A4 on the default voice and measure; returns {transpose, semitones}."""
        known = self.get(chip, chan)
        if known is not None:
            return known
        result = {"transpose": 0, "semitones": None}
        if np is not None:
            timing = Timing(60.0, 6, 0.5, 0.1, 0.1, "off")
            sounding = [parse_note("A3"), parse_note("E4"), parse_note("A4")]
            sample = saw_sample(0.5, note_hz(sounding[-1])) if chip["sampleFormatMask"] else None
            fur_path, wav_path = self.fur.temp(".fur"), self.fur.temp(".wav")
            try:
                with open(fur_path, "wb") as f:
                    f.write(build_fur(self.fur.version, chip, timing, {chan: sounding}, "probe", "", sample=sample))
                self.fur.run(fur_path, "-output", wav_path, "-outformat", "s16")
                schedule = timing.schedule(sounding, 0)
                cents = measure(wav_path, schedule)
            finally:
                os.unlink(fur_path)
                os.unlink(wav_path)
            if cents is not None:
                semis = cents / 100
                octaves = round(semis / 12)
                result["semitones"] = round(semis, 2)
                if octaves and abs(semis - 12 * octaves) < 0.5:
                    result["transpose"] = -12 * octaves
        with self.lock:
            self.data[self.key(chip, chan)] = result
        return result

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with self.lock, open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=1, sort_keys=True)


# --------------------------------------------------------------------------
# WAV post-processing: mono fold, labels, cue points


def chunk(fourcc, payload):
    data = fourcc + struct.pack("<I", len(payload)) + payload
    return data + b"\0" if len(payload) % 2 else data


def zstr(text):
    return text.encode("utf-8") + b"\0"


def finish_wav(path, title, comment, schedule, keep_stereo, software):
    """Rewrite a Furnace WAV with labels and cue points; returns stats for the manifest."""
    with wave.open(path, "rb") as w:
        nch, width, rate, frames = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        raw = w.readframes(frames)
    if width != 2:
        raise RuntimeError(f"{path}: expected 16-bit output, got {width * 8}-bit")
    pcm = array.array("h")
    pcm.frombytes(raw)
    if sys.byteorder != "little":
        pcm.byteswap()
    peak = max(max(pcm, default=0), -min(pcm, default=0))
    if nch == 2 and not keep_stereo and pcm[0::2] == pcm[1::2]:
        pcm = pcm[0::2]
        nch = 1
    if sys.byteorder != "little":
        pcm.byteswap()

    fmt = struct.pack("<HHIIHH", 1, nch, rate, rate * nch * 2, nch * 2, 16)
    cue = struct.pack("<I", len(schedule))
    labels = b""
    for i, s in enumerate(schedule, 1):
        pos = min(frames, round(s["onset_sec"] * rate))
        cue += struct.pack("<II4sIII", i, pos, b"data", 0, 0, pos)
        labels += chunk(b"labl", struct.pack("<I", i) + zstr(s["note"]))
    info = chunk(b"INAM", zstr(title)) + chunk(b"ICMT", zstr(comment)) + chunk(b"ISFT", zstr(software))
    body = (
        b"WAVE" + chunk(b"fmt ", fmt) + chunk(b"data", pcm.tobytes()) + chunk(b"cue ", cue)
        + chunk(b"LIST", b"adtl" + labels) + chunk(b"LIST", b"INFO" + info)
    )
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(b"RIFF" + struct.pack("<I", len(body)) + body)
    os.replace(tmp, path)
    return {
        "channels": nch,
        "rate": rate,
        "seconds": round(frames / rate, 3),
        "peak_dbfs": round(20 * math.log10(peak / 32768), 2) if peak else None,
        "silent": peak == 0,
    }


# --------------------------------------------------------------------------
# jobs


class Job:
    def __init__(self, ctx):
        self.ctx = ctx

    def pitch_comment(self, transpose):
        if not transpose:
            return ""
        return (f" Furnace sounds this channel {abs(transpose) // 12} octave(s) {'above' if transpose < 0 else 'below'} "
                f"its note names, so the notes are written {abs(transpose)} semitones "
                f"{'lower' if transpose < 0 else 'higher'} to sound at the pitches above.")

    def finish(self, wav, fur_path, title, schedule, **extra):
        ctx = self.ctx
        stats = finish_wav(wav, title, ctx.timing.describe(ctx.sounding), schedule, ctx.args.keep_stereo, ctx.fur.software)
        cents = measure(wav, schedule) if np is not None and not stats["silent"] else None
        return dict(
            wav=os.path.relpath(wav, ctx.args.out), fur=os.path.relpath(fur_path, ctx.args.out),
            settings=ctx.settings, pitch_error_cents=cents, schedule=schedule, **extra, **stats,
        )


def chip_label(chip):
    return slug(chip["name"])


def pick_channels(chip, mode):
    if mode == "first":
        return [0]
    if mode == "all":
        return list(range(chip["channels"]))
    seen, picked = set(), []
    for i, c in enumerate(chip["chans"]):
        key = (c["type"], c["ins"][0])
        if key not in seen:
            seen.add(key)
            picked.append(i)
    return picked


def preset_target(chips_by_id, chips, ins_type, rel_path):
    """Chip and channel for a preset: the type's chip, preferring a channel named in the file name."""
    if ins_type in PRESET_CHIP and PRESET_CHIP[ins_type] in chips_by_id:
        chip = chips_by_id[PRESET_CHIP[ins_type]]
    else:
        chip = next((c for c in chips if any(ins_type in ch["ins"] for ch in c["chans"])), None)
        if chip is None:
            return None, None
    usable = [i for i, ch in enumerate(chip["chans"]) if ins_type in ch["ins"]] or [0]
    words = set(re.findall(r"[a-z]+", rel_path.lower()))
    for i in usable:
        kind = re.sub(r"[^a-z]", "", chip["chans"][i]["name"].lower())
        if kind and kind in words:
            return chip, i
    for slot in (0, 1):
        for i in usable:
            if chip["chans"][i]["ins"][slot] == ins_type:
                return chip, i
    return chip, usable[0]


def preset_name(rel, index, count, ins_name, keep_ext=False):
    stem, ext = os.path.splitext(rel)
    name = slug(stem.replace(os.sep, "__")) + (f"-{ext[1:]}" if keep_ext else "")
    if count > 1:
        name += f"__{index:02d}-{slug(ins_name)}"
    return name


def render_chip(ctx, chip):
    args, timing = ctx.args, ctx.timing
    label = chip_label(chip)
    play = pick_channels(chip, args.channels)
    perchan = args.channels != "first"
    base = os.path.join(args.out, "chips", label) if perchan else os.path.join(args.out, "chips")
    os.makedirs(base, exist_ok=True)
    fur_path = os.path.join(base, label + ".fur")
    shifts = {c: ctx.calibration.probe(chip, c)["transpose"] for c in play}
    voices = {c: [n + shifts[c] for n in ctx.sounding] for c in play}
    sample = saw_sample(args.note_sec, note_hz(ctx.sounding[-1])) if chip["sampleFormatMask"] else None
    comment = timing.describe(ctx.sounding) + f" Chip: {chip['name']}. " + (
        "Instrument 0: a blank instrument (this chip needs one to play samples)." if chip["id"] in NEEDS_INSTRUMENT
        else "No instrument (Furnace default voice).")
    if sample:
        comment += " Sample 0: looped saw for sample-based channels."
    for c in play:
        if shifts[c]:
            comment += f" Channel {c + 1} ({chip['chans'][c]['name']}):" + Job(ctx).pitch_comment(shifts[c])
    with open(fur_path, "wb") as f:
        f.write(build_fur(ctx.fur.version, chip, timing, voices, f"{chip['name']} chromatic run", comment, sample=sample,
                          blank_ins=chip["chans"][play[0]]["ins"][0] if chip["id"] in NEEDS_INSTRUMENT else None))

    wav_path = os.path.join(base, label + ".wav")
    if perchan:
        ctx.fur.run(fur_path, "-output", wav_path, "-outformat", "s16", "-outmode", "perchan")
        outputs = []
        for c in range(chip["channels"]):
            produced = os.path.join(base, f"{label}_c{c + 1:02d}.wav")
            if c not in play:
                if os.path.exists(produced):
                    os.unlink(produced)
                continue
            name = chip["chans"][c]["name"]
            final = os.path.join(base, f"{label}__ch{c + 1:02d}-{slug(name)}.wav")
            os.replace(produced, final)
            outputs.append((final, c))
    else:
        ctx.fur.run(fur_path, "-output", wav_path, "-outformat", "s16")
        outputs = [(wav_path, 0)]

    entries = []
    for path, c in outputs:
        cname = chip["chans"][c]["name"]
        title = f"{chip['name']} - {cname} - default voice" if perchan else f"{chip['name']} - default voice"
        entries.append(Job(ctx).finish(
            path, fur_path, title, timing.schedule(ctx.sounding, shifts[c]),
            kind="chip", chip=chip["name"], chip_id=f"0x{chip['id']:02x}", channel=c + 1, channel_name=cname,
            transpose=shifts[c], preset=None,
        ))
    return entries


def preset_octaves(entry):
    """Whole octaves a preset sounds away from the target, if clean and consistent; else 0."""
    cents = entry["pitch_error_cents"]
    if cents is None:
        return 0
    k = round(cents / 1200)
    if not k or abs(k) > 3 or abs(cents - 1200 * k) > 30:
        return 0
    agree = sum(1 for s in entry["schedule"]
                if s["measured_hz"] and abs(1200 * math.log2(s["measured_hz"] / s["hz"]) - 1200 * k) < 50)
    return k if agree >= 0.6 * len(entry["schedule"]) else 0


def render_preset(ctx, job):
    """Render one preset; if it is voiced whole octaves away from the target, re-render it transposed."""
    args, timing = ctx.args, ctx.timing
    chip, chan, path, index, ins, count = job["chip"], job["chan"], job["path"], job["index"], job["ins"], job["count"]
    rel = os.path.relpath(path, args.preset_dir)
    base = os.path.join(args.out, "presets", chip_label(chip))
    os.makedirs(base, exist_ok=True)
    name = preset_name(rel, index, count, ins["name"], job["keep_ext"])
    fur_path = os.path.join(base, name + ".fur")
    wav_path = os.path.join(base, name + ".wav")
    chip_shift = ctx.calibration.probe(chip, chan)["transpose"]
    cname = chip["chans"][chan]["name"]
    ins_type = INS_TYPES[ins["type"]] if ins["type"] < len(INS_TYPES) else ins["type"]

    preset_shift = 0
    for attempt in range(2):
        shift = chip_shift + preset_shift
        comment = (timing.describe(ctx.sounding) + f" Chip: {chip['name']}, channel {chan + 1} ({cname}). "
                   f"Instrument 0: preset {rel}" + (f" #{index}" if count > 1 else "") + f" ({ins['name']})."
                   + Job(ctx).pitch_comment(chip_shift))
        if preset_shift:
            comment += (f" The preset itself is voiced {abs(preset_shift) // 12} octave(s) "
                        f"{'below' if preset_shift > 0 else 'above'} its notes, so it is written {abs(preset_shift)} "
                        f"semitones {'higher' if preset_shift > 0 else 'lower'} as well (chipruns --native-octave keeps its register).")
        base_fur = ctx.fur.temp(".fur")
        try:
            with open(base_fur, "wb") as f:
                f.write(build_fur(ctx.fur.version, chip, timing, {chan: [n + shift for n in ctx.sounding]},
                                  f"{ins['name']} on {chip['name']}", comment, ins=0))
            ctx.fur.run(base_fur, "-insfile", path, "-insindex", str(index), "-saveas", fur_path)
        finally:
            os.unlink(base_fur)
        ctx.fur.run(fur_path, "-output", wav_path, "-outformat", "s16")

        entry = Job(ctx).finish(
            wav_path, fur_path, f"{chip['name']} - {ins['name']} ({rel})", timing.schedule(ctx.sounding, shift),
            kind="preset", chip=chip["name"], chip_id=f"0x{chip['id']:02x}", channel=chan + 1, channel_name=cname,
            transpose=shift, preset_octave_shift=preset_shift // 12,
            preset=dict(file=rel, index=index, name=ins["name"], type=ins_type),
        )
        if attempt or args.native_octave or np is None:
            break
        octaves = preset_octaves(entry)
        if not octaves:
            break
        preset_shift = -12 * octaves
    return [entry]


def scan_presets(fur, preset_dir, cache_path, jobs):
    cache = {}
    if os.path.exists(cache_path):
        with open(cache_path, encoding="utf-8") as f:
            cache = json.load(f)
    files = []
    for root, dirs, names in os.walk(preset_dir):
        dirs.sort()
        for n in sorted(names):
            if n.rsplit(".", 1)[-1].lower() in INS_EXTS:
                files.append(os.path.join(root, n))

    def key(p):
        st = os.stat(p)
        return f"{fur.version_name}|{p}|{st.st_size}|{int(st.st_mtime)}"

    todo = [p for p in files if key(p) not in cache]
    failed = []
    if todo:
        print(f"reading {len(todo)} preset files...", flush=True)
        with ThreadPoolExecutor(jobs) as ex:
            futures = {ex.submit(fur.instruments, p): p for p in todo}
            for fut in as_completed(futures):
                p = futures[fut]
                try:
                    cache[key(p)] = fut.result()
                except RuntimeError as e:
                    failed.append((p, str(e)))
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(cache, f)
    for p, err in failed:
        print(f"  could not read {p}: {err}", file=sys.stderr)
    return [(p, cache[key(p)]) for p in files if key(p) in cache]


def write_manifest(path, manifest):
    """Write entries whose WAV still exists (renamed or deleted files drop out)."""
    root = os.path.dirname(path)
    entries = [e for e in manifest.values() if os.path.exists(os.path.join(root, e["wav"]))]
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(sorted(entries, key=lambda e: e["wav"]), f, indent=1, ensure_ascii=False)
    os.replace(tmp, path)


def main():
    doc = __doc__.split("\n\n")
    ap = argparse.ArgumentParser(description=doc[0], epilog="\n\n".join(doc[1:]),
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target", nargs="?", choices=("all", "chips", "presets"), default="all",
                    help="what to render (default: all)")
    ap.add_argument("--out", default=os.path.join(HERE, "out"), help="output directory (default: ./out)")
    ap.add_argument("--filter", help="regex on chip name (chips) or chip name + preset path (presets)")
    ap.add_argument("--low", type=parse_note, default=parse_note("A3"), help="lowest sounding note (default A3 = 220 Hz)")
    ap.add_argument("--high", type=parse_note, default=parse_note("A4"), help="highest sounding note (default A4 = 440 Hz)")
    ap.add_argument("--note-sec", type=float, default=1.0, help="note length in seconds (default 1.0)")
    ap.add_argument("--rest-sec", type=float, default=0.5, help="rest between notes in seconds (default 0.5)")
    ap.add_argument("--tail-sec", type=float, default=1.0, help="extra silence after the last rest (default 1.0)")
    ap.add_argument("--tick-rate", type=float, default=60.0, help="engine ticks per second (default 60)")
    ap.add_argument("--speed", type=int, default=6, help="ticks per row (default 6, i.e. 0.1 s rows at 60 Hz)")
    ap.add_argument("--note-end", choices=("off", "release"), default="off",
                    help="off: key off at note end (FM releases, PSG cuts); "
                         "release: === at note end, then OFF just before the next note (lets macro releases play)")
    ap.add_argument("--channels", choices=("first", "types", "all"), default="first",
                    help="chip runs: play channel 1 only, the first channel of each kind, or every channel "
                         "(the last two render each channel to its own WAV)")
    ap.add_argument("--variants", action="store_true",
                    help="also render chip variants (extended channel 3, CSM, DualPCM, drum modes)")
    ap.add_argument("--no-transpose", action="store_true",
                    help="write A-3..A-4 as-is even where Furnace sounds them an octave or two off")
    ap.add_argument("--native-octave", action="store_true",
                    help="presets: keep each preset's own register instead of moving it to the requested octave")
    ap.add_argument("--rate", type=int, default=44100, help="WAV sample rate (default 44100)")
    ap.add_argument("--keep-stereo", action="store_true", help="keep stereo even when both sides are identical")
    ap.add_argument("--jobs", type=int, default=os.cpu_count() or 4, help="parallel renders")
    ap.add_argument("--furnace", default=DEFAULT_FURNACE, help="patched Furnace binary (default: bin/furnace-batch)")
    ap.add_argument("--preset-dir", default=PRESET_DIR, help=f"preset directory (default: {PRESET_DIR})")
    ap.add_argument("--force", action="store_true", help="re-render files that already exist with the same settings")
    ap.add_argument("--list", action="store_true", help="list what would be rendered and exit")
    ap.add_argument("--remeasure", action="store_true",
                    help="re-run the pitch measurement on every WAV in the manifest (no rendering) and exit")
    args = ap.parse_args()
    args.out = os.path.abspath(args.out)
    args.preset_dir = os.path.abspath(args.preset_dir)
    if args.high < args.low:
        raise SystemExit("--high must not be below --low")
    if np is None and not args.no_transpose:
        print("numpy not found: pitch probing is off, notes are written as named (NES, PCE, SCC and SN76489 "
              "will sound one or two octaves high). Install numpy or pass --no-transpose to silence this.", file=sys.stderr)

    settings = {k: getattr(args, k) for k in ("note_sec", "rest_sec", "tail_sec", "tick_rate", "speed", "note_end",
                                              "rate", "keep_stereo", "no_transpose", "native_octave")}
    settings.update(low=pitch_name(args.low), high=pitch_name(args.high))
    rx = re.compile(args.filter, re.I) if args.filter else None

    os.makedirs(args.out, exist_ok=True)
    workdir = tempfile.mkdtemp(prefix="chipruns-")
    try:
        fur = Furnace(os.path.abspath(args.furnace), args.rate, workdir)
        ctx = type("Ctx", (), {})()
        ctx.args, ctx.fur, ctx.settings = args, fur, settings
        ctx.timing = Timing(args.tick_rate, args.speed, args.note_sec, args.rest_sec, args.tail_sec, args.note_end)
        ctx.sounding = list(range(args.low, args.high + 1))
        ctx.calibration = Calibration(fur, args, os.path.join(args.out, ".cache", "calibration.json"))
        if args.no_transpose:
            ctx.calibration.probe = lambda chip, chan: {"transpose": 0, "semitones": None}

        all_chips = [c for c in fur.chips() if not c["compound"] and c["id"] and c["chans"]]
        chips_by_id = {c["id"]: c for c in all_chips}
        base_chips = [c for c in all_chips if args.variants or not VARIANT_RE.search(c["name"])]

        manifest_path = os.path.join(args.out, "manifest.json")
        manifest = {}
        if os.path.exists(manifest_path):
            with open(manifest_path, encoding="utf-8") as f:
                manifest = {e["wav"]: e for e in json.load(f)}
        if args.remeasure:
            if np is None:
                raise SystemExit("--remeasure needs numpy")
            entries = [e for e in manifest.values() if not e["silent"] and os.path.exists(os.path.join(args.out, e["wav"]))]

            def redo(e):
                e["pitch_error_cents"] = measure(os.path.join(args.out, e["wav"]), e["schedule"])

            with ThreadPoolExecutor(args.jobs) as ex:
                list(ex.map(redo, entries))
            write_manifest(manifest_path, manifest)
            print(f"re-measured {len(entries)} files; manifest: {manifest_path}")
            return

        key_settings = json.dumps(settings, sort_keys=True)
        done = {e["fur"] for e in manifest.values()
                if json.dumps(e["settings"], sort_keys=True) == key_settings and os.path.exists(os.path.join(args.out, e["wav"]))}

        tasks = []
        if args.target in ("all", "chips"):
            for chip in base_chips:
                if rx and not rx.search(chip["name"]):
                    continue
                label = chip_label(chip)
                fur_rel = os.path.join("chips", label, label + ".fur") if args.channels != "first" else os.path.join("chips", label + ".fur")
                if not args.force and fur_rel in done:
                    continue
                tasks.append(("chip", chip["name"], lambda chip=chip: render_chip(ctx, chip)))

        if args.target in ("all", "presets"):
            presets = scan_presets(fur, args.preset_dir, os.path.join(args.out, ".cache", "presets.json"), args.jobs)
            stems = {}
            for path, _ in presets:
                stem = os.path.splitext(os.path.relpath(path, args.preset_dir))[0]
                stems[stem] = stems.get(stem, 0) + 1
            for path, inss in presets:
                rel = os.path.relpath(path, args.preset_dir)
                keep_ext = stems[os.path.splitext(rel)[0]] > 1  # e.g. Banjo.dmp and Banjo.opni
                for index, ins in enumerate(inss):
                    if len(inss) > 1 and ins["name"] == "no Name":
                        continue  # empty slot in a VOPM (.opm) bank
                    chip, chan = preset_target(chips_by_id, all_chips, ins["type"], rel)
                    if chip is None:
                        print(f"  no chip plays instrument type {ins['type']} ({rel}); skipped", file=sys.stderr)
                        continue
                    if rx and not rx.search(f"{chip['name']} {rel}"):
                        continue
                    fur_rel = os.path.join("presets", chip_label(chip), preset_name(rel, index, len(inss), ins["name"], keep_ext) + ".fur")
                    if not args.force and fur_rel in done:
                        continue
                    job = dict(chip=chip, chan=chan, path=path, index=index, ins=ins, count=len(inss), keep_ext=keep_ext)
                    desc = f"{chip['name']} ch{chan + 1}: {rel}" + (f" #{index}" if len(inss) > 1 else "")
                    tasks.append(("preset", desc, lambda job=job: render_preset(ctx, job)))

        seconds = ctx.timing.seconds(len(ctx.sounding))
        if args.list:
            for kind, desc, _ in tasks:
                print(f"{kind:6} {desc}")
            print(f"{len(tasks)} renders, {seconds:.1f} s each")
            return

        print(f"rendering {len(tasks)} runs ({seconds:.1f} s each) with {args.jobs} jobs into {args.out}", flush=True)
        failures = []
        with ThreadPoolExecutor(args.jobs) as ex:
            futures = {ex.submit(fn): (kind, desc) for kind, desc, fn in tasks}
            for i, fut in enumerate(as_completed(futures), 1):
                kind, desc = futures[fut]
                try:
                    for entry in fut.result():
                        manifest[entry["wav"]] = entry
                        notes = []
                        if entry["silent"]:
                            notes.append("silent")
                        if entry["transpose"]:
                            notes.append(f"written {entry['transpose']:+d}")
                        if entry.get("preset_octave_shift"):
                            notes.append(f"preset voiced {-entry['preset_octave_shift']:+d} oct")
                        if entry["pitch_error_cents"] is not None and abs(entry["pitch_error_cents"]) > 50:
                            notes.append(f"pitch {entry['pitch_error_cents']:+.0f} cents")
                        print(f"[{i}/{len(tasks)}] {entry['wav']}" + (f" ({', '.join(notes)})" if notes else ""), flush=True)
                except Exception as e:  # keep going; report at the end
                    failures.append((desc, f"{type(e).__name__}: {e}"))
                    print(f"[{i}/{len(tasks)}] FAILED {desc}: {e}", flush=True)
                if i % 50 == 0:
                    write_manifest(manifest_path, manifest)
                    ctx.calibration.save()
        write_manifest(manifest_path, manifest)
        ctx.calibration.save()

        silent = sorted(e["wav"] for e in manifest.values() if e["silent"])
        print(f"done: {len(tasks) - len(failures)} ok, {len(failures)} failed; manifest: {manifest_path}")
        if silent:
            print(f"{len(silent)} silent renders (chip or preset produced no output):")
            for s in silent:
                print("  " + s)
        for desc, err in failures:
            print(f"  failed: {desc}: {err}", file=sys.stderr)
        sys.exit(1 if failures else 0)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    main()
