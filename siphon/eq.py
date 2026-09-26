"""The equalizer: ten octave bands, built-in and saved presets, and the mpv filter chain that plays them.

The model is plain data kept in settings.json with the other choices. The player plays chain(gains()) and moves a
playing chain with commands().
"""

import cmath
import functools
import math
import unicodedata
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field

FREQUENCIES = (31, 62, 125, 250, 500, 1000, 2000, 4000, 8000, 16000)  # Hz, an octave apart
LABELS = ("31", "62", "125", "250", "500", "1k", "2k", "4k", "8k", "16k")
MIN_DB, MAX_DB = -12.0, 12.0  # the usual range of a graphic equalizer (EasyEffects' too)
STEP_DB = 0.5  # half the ~1 dB smallest level change people hear, so a slider step never jumps audibly
FLAT = (0.0,) * len(FREQUENCIES)
BUILT_IN: dict[str, tuple[float, ...]] = {
    "Flat": FLAT,
    "Bass": (5.0, 6.0, 5.0, 3.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0),
    "Treble": (0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 3.0, 5.0, 6.0, 6.0),
    "Vocal": (-2.0, -1.0, 0.0, 2.0, 4.0, 4.0, 3.0, 1.0, 0.0, -1.0),
    "Pop": (-1.0, 0.0, 2.0, 4.0, 4.0, 2.0, 0.0, -1.0, -1.0, -1.0),
    "Rock": (5.0, 4.0, 2.0, -1.0, -2.0, -1.0, 2.0, 4.0, 5.0, 5.0),
    "Jazz": (4.0, 3.0, 1.0, 2.0, -1.0, -1.0, 0.0, 1.0, 3.0, 4.0),
    "Classic": (4.0, 3.0, 2.0, 0.0, -1.0, -1.0, 0.0, 2.0, 3.0, 4.0),
}
CUSTOM = "Custom"  # what `preset` says once the bands match no preset
NAME_LIMIT = 40  # characters in a preset name
PREAMP = "preamp"  # mpv label of the volume filter in front of the bands
PREAMP_STEP_DB = 0.5  # the most one step() moves the preamp
FILTERS = ("rate", PREAMP, *(f"eq{i}" for i in range(len(FREQUENCIES))))  # chain()'s mpv labels, in order
# ffmpeg's equalizer turns every sample into NaN when a band sits exactly at half the sample rate (16 kHz in a 32 kHz
# file, 8 kHz at 16 kHz, 4 kHz at 8 kHz; measured, ffmpeg 9.0.2), so slower files are resampled first, to the nearest
# of these; headroom() works the curve out at the two lowest, where it peaks highest.
_RATES_IN = "44100|48000|88200|96000|176400|192000|352800|384000"


_RESERVED = {name.casefold() for name in (*BUILT_IN, CUSTOM)}


def snap(db: float) -> float:
    """The nearest step inside the range (+ 0.0 turns -0.0 into 0.0)."""
    return min(MAX_DB, max(MIN_DB, round(db / STEP_DB) * STEP_DB)) + 0.0


def clean_name(name: str) -> str:
    """A preset name as stored: runs of spaces, tabs and newlines become one space. Raises ValueError, saying why."""
    name = " ".join(name.split())
    if not name:
        raise ValueError("Give the preset a name.")
    if len(name) > NAME_LIMIT:
        raise ValueError(f"A preset name can have at most {NAME_LIMIT} characters.")
    if any(unicodedata.category(c) == "Cc" for c in name):
        raise ValueError("A preset name can't contain control characters.")
    if name.casefold() in _RESERVED:
        raise ValueError(f"“{name}” is a built-in name; choose another.")
    return name


@dataclass
class Equalizer:
    """What the equalizer page shows and the player plays. Edit it only through the methods (and `enabled`)."""

    enabled: bool = True  # with Flat bands on, nothing runs: a preset picked sounds at once without a switch
    bands: tuple[float, ...] = FLAT  # dB per band, each a step inside the range
    preset: str = "Flat"  # a built-in or saved name whose curve the bands are, else CUSTOM
    presets: dict[str, tuple[float, ...]] = field(default_factory=dict)  # the user's own, in the order saved

    def names(self) -> list[str]:
        """Every preset, the built-in ones first."""
        return [*BUILT_IN, *self.presets]

    def gains(self) -> tuple[float, ...] | None:
        """What the player should play: None while switched off or flat, which runs no filter at all."""
        return self.bands if self.enabled and any(self.bands) else None

    def set_band(self, index: int, db: float) -> None:
        """Move one band to the nearest step; `preset` becomes whichever preset the bands now are, else CUSTOM."""
        bands = list(self.bands)
        bands[index] = snap(db)
        self.bands = tuple(bands)
        self.preset = self._match()

    def apply(self, name: str) -> None:
        """Take the named preset's curve. KeyError when there is none."""
        self.bands = self._curve(name)
        self.preset = name

    def find(self, name: str) -> str | None:
        """The saved preset that save(name) would replace (names ignore case), so the page can ask first."""
        key = " ".join(name.split()).casefold()
        return next((saved for saved in self.presets if saved.casefold() == key), None)

    def save(self, name: str) -> str:
        """Keep the bands as the user's preset `name`, replacing find(name) in its place; returns the name kept."""
        name = clean_name(name)
        self.presets = dict(_replace(self.presets, self.find(name), name, self.bands))
        self.preset = name
        return name

    def rename(self, old: str, new: str) -> str:
        """Returns the name kept. KeyError when `old` is not a saved preset, ValueError when `new` is unusable or
        taken."""
        curve = self.presets[old]
        new = clean_name(new)
        if (taken := self.find(new)) not in (None, old):
            raise ValueError(f"There is already a preset called “{taken}”.")
        self.presets = dict(_replace(self.presets, old, new, curve))
        if self.preset == old:
            self.preset = new
        return new

    def delete(self, name: str) -> None:
        """KeyError when `name` is not a saved preset; the bands stay as they are."""
        del self.presets[name]
        if self.preset == name:
            self.preset = self._match()

    def _match(self) -> str:
        return next((name for name in self.names() if self._curve(name) == self.bands), CUSTOM)

    def _curve(self, name: str) -> tuple[float, ...]:
        return BUILT_IN[name] if name in BUILT_IN else self.presets[name]


def _replace(presets: dict[str, tuple[float, ...]], old: str | None, new: str,
             curve: tuple[float, ...]) -> Iterator[tuple[str, tuple[float, ...]]]:
    """The presets with `old` swapped for `new` in its place, or `new` added at the end when `old` is None."""
    for name, saved in presets.items():
        yield (new, curve) if name == old else (name, saved)
    if old is None:
        yield new, curve


def _curve_from_json(value: object) -> tuple[float, ...] | None:
    def number(db: object) -> bool:
        return isinstance(db, (int, float)) and not isinstance(db, bool) and math.isfinite(db)  # `true` is no gain

    if not isinstance(value, list) or len(value) != len(FREQUENCIES) or not all(map(number, value)):
        return None
    return tuple(snap(db) for db in value)


def from_json(data: object, defaults: Equalizer) -> Equalizer:
    """The equalizer saved in settings.json; anything missing or damaged falls back to `defaults`, one value at a time
    (a preset with a bad name or curve is dropped on its own)."""
    if not isinstance(data, dict):
        return defaults
    enabled, bands = data.get("enabled"), _curve_from_json(data.get("bands"))
    saved = data.get("presets")
    result = Equalizer(enabled if isinstance(enabled, bool) else defaults.enabled,
                       defaults.bands if bands is None else bands, CUSTOM,
                       {} if isinstance(saved, dict) else dict(defaults.presets))
    for name, value in saved.items() if isinstance(saved, dict) else ():
        try:
            name = clean_name(name)
        except ValueError:
            continue
        curve = _curve_from_json(value)
        if curve is not None and result.find(name) is None:
            result.presets[name] = curve
    preset = data.get("preset")
    # a name kept only while it still names these bands: two presets can share a curve, and the one picked shows
    in_use = isinstance(preset, str) and preset in result.names() and result._curve(preset) == result.bands
    result.preset = preset if in_use else result._match()
    return result


# -- mpv's side -----------------------------------------------------------------------------------------------------

def chain(gains: Sequence[float] | None) -> str:
    """mpv's `af` value for these gains: a sample rate the bands work at, a preamp, then one octave-wide peaking
    filter per band (ffmpeg's equalizer).

    Empty when off or flat, so a flat equalizer costs nothing. All ten bands are there while any is not flat, so a
    slider can move any of them through commands() without rebuilding the chain.
    """
    return filters(gains) if gains is not None and any(gains) else ""


def filters(gains: Sequence[float]) -> str:
    """chain() even when flat: then every filter is at 0 dB, which passes the sound through exactly.

    mpv runs each filter in an ffmpeg graph of its own, and ffmpeg gives every graph a pool of threads, one per core
    up to 16, to which the band filters hand each block's two channels. So each graph here is held to one thread:
    the same samples bit for bit, but on a 24-core computer 12 threads instead of 192, about 110 wakeups a second
    instead of 890, and half the CPU time (Rock on a 44.1 kHz MP3, measured 2026-09-26, mpv 0.41, ffmpeg 9).
    """
    bands = [(f"eq{i}", f"equalizer=f={hz}:t=o:w=1:g={db:g}") for i, (hz, db) in enumerate(zip(FREQUENCIES, gains))]
    return ",".join(f"@{label}:lavfi=graph=[{spec}]:o=[threads=1]" for label, spec in (
        (FILTERS[0], f"aformat=sample_rates={_RATES_IN}"), (PREAMP, f"volume=volume={headroom(gains):g}dB"), *bands))


def step(here: Sequence[float], target: Sequence[float]) -> tuple[float, ...]:
    """The next curve on the way from `here` to `target` whose preamp is at most PREAMP_STEP_DB from here's.

    Changed by commands, a band moves smoothly (its filter keeps its state) but the preamp, a plain gain, jumps. On a
    100 Hz tone at 0.25 of full scale, played in real time, the sharpest bend in the wave against a clean sine's at
    +12 dB, over two runs: Rock switched off in one jump 520x, in these steps 37-58x; Rock to Custom 182-251x, stepped
    49-55x. The way is a straight line in dB per band, cut short where the curve would peak higher on it.
    """
    here, target = tuple(here), tuple(target)
    # rounded: headroom() gives tenths, and -8.3 - -7.8 is 0.5000000000000009, which would make two steps of one
    count = math.ceil(round(abs(headroom(target) - headroom(here)), 1) / PREAMP_STEP_DB)
    nxt = target if count <= 1 else tuple(a + (b - a) / count for a, b in zip(here, target))
    while abs(headroom(nxt) - headroom(here)) > PREAMP_STEP_DB + 0.1:  # + headroom()'s rounding
        nxt = tuple((a + b) / 2 for a, b in zip(here, nxt))
    return nxt


def commands(old: Sequence[float], new: Sequence[float]) -> list[tuple[str, str, str, str]]:
    """mpv af-command arguments (label, command, argument, target) that turn a running chain(old) into chain(new).

    The target names the ffmpeg filter: with mpv's default target "all", ffmpeg answers with the last filter in the
    graph, which takes no commands, so mpv reports a failure even though the change was made (mpv 0.41, ffmpeg 9.0).
    The preamp moves first when it turns the sound down and last when it turns it up, so no block between two
    commands is louder than either chain.
    """
    bands = [(f"eq{i}", "gain", f"{b:g}", "equalizer") for i, (a, b) in enumerate(zip(old, new)) if a != b]
    before, after = headroom(old), headroom(new)
    if before == after:
        return bands
    preamp = [(PREAMP, "volume", f"{after:g}dB", "volume")]
    return preamp + bands if after < before else bands + preamp


# The sample rates downloads decode at (Opus always at 48 kHz). The bilinear transform squeezes the top bands' bells
# toward the Nyquist frequency, so the lower rate peaks higher: Treble 9.28 dB at 44.1 kHz, 8.96 dB at 48 kHz.
_RATES = (44100, 48000)
# grid points per octave from 20 Hz; a parabola through the top points finds the peak between them, to within
# 0.0023 dB of a 1/384-octave search refined by golden section (the worst of 150 random curves)
_PER_OCTAVE = 12
_GRID = tuple(20 * 2 ** (k / _PER_OCTAVE) for k in range(10 * _PER_OCTAVE + 1))  # 20 Hz - 20.48 kHz


def response(gains: Sequence[float], hz: float, rate: int) -> float:
    """The bands' combined gain in dB at `hz`, by the formulas ffmpeg's equalizer uses (the RBJ cookbook peaking
    biquad, bandwidth in octaves)."""
    z = cmath.exp(-2j * math.pi * hz / rate)
    total = 0.0
    for f0, db in zip(FREQUENCIES, gains):
        if db:
            w0 = 2 * math.pi * f0 / rate
            alpha = math.sin(w0) * math.sinh(math.log(2) / 2 * w0 / math.sin(w0))  # width 1 octave
            a, c = 10 ** (db / 40), -2 * math.cos(w0)
            total += 20 * math.log10(abs(1 + alpha * a + c * z + (1 - alpha * a) * z * z)
                                     / abs(1 + alpha / a + c * z + (1 - alpha / a) * z * z))
    return total


@functools.lru_cache(maxsize=64)
def _headroom(gains: tuple[float, ...]) -> float:
    peak = 0.0
    for rate in _RATES:
        levels = [response(gains, hz, rate) for hz in _GRID]
        top = max(levels)
        for k in range(1, len(levels) - 1):
            # every local peak within 0.1 dB of the highest point: a slightly lower one may hide a higher top
            left, mid, right = levels[k - 1], levels[k], levels[k + 1]
            if mid >= left and mid >= right and mid > top - 0.1 and left + right != 2 * mid:
                shift = 0.5 * (left - right) / (left - 2 * mid + right)
                top = max(top, response(gains, _GRID[k] * 2 ** (shift / _PER_OCTAVE), rate))
        peak = max(peak, top)
    if peak <= 1e-9:  # cuts only: nothing rises above 0 dB but float noise
        return 0.0
    # rounded before ceil: float noise (12.000000000000002) must not cost another 0.1 dB
    return -math.ceil(round((peak + _MARGIN_DB) * 10, 6)) / 10 + 0.0


# ffmpeg runs these filters in 32-bit float: a lone +12 dB band under a preamp of exactly -12.0 dB turned a
# full-scale tone at its centre into a 1.0001 peak (measured, ffmpeg 9.0.2); this much more keeps it under 1.0.
_MARGIN_DB = 0.05


def headroom(gains: Sequence[float]) -> float:
    """The preamp in dB (0 or less, a multiple of 0.1) that keeps the loudest point of the curve below 0 dB.

    Neighbouring bells overlap, so the curve can rise well above the highest slider: Bass (+6 at most) peaks at
    7.96 dB and every band at +12 at 19.64 dB, so a preamp of minus the highest slider would still clip.
    """
    return _headroom(tuple(gains))
