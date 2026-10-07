# furnace-chipruns

**Free WAV samples of 80+ emulated sound chips and 1,280+ instrument presets**,
each playing a labelled chromatic run (A3 to A4), rendered with
[Furnace](https://github.com/tildearrow/furnace)'s own chip emulators. Grab them
and use them in your own tracks, samplers, trackers, or tests.

## Download the WAV samples

| What | Count | Where |
| --- | --- | --- |
| Chip default voices | 83 chips | [`samples/chips/`](samples/chips) (in this repo) |
| Instrument presets | 1,282 presets on 19 chip families | [Releases](../../releases/latest), one zip per chip |
| Pitch-measurement manifest | every file above | [`samples/manifest.json`](samples/manifest.json) |

- **Chips:** browse and download straight from [`samples/chips/`](samples/chips),
  or take everything in one go from `chips-all.zip` on the Releases page.
- **Presets:** about 2.6 GB of WAVs, so they ship as release assets
  (`presets-<chip>.zip`) rather than in git. Download only the chips you want.
- Every WAV is 44.1 kHz and carries a `LIST/INFO` title (chip and preset) and a
  cue point named after the sounding note at each note onset, so they slice
  cleanly in most samplers and DAWs.
- Each `.wav` has a matching `.fur` project you can open in Furnace.
- Pitches are verified: the manifest records the written note, the target
  frequency, and the measured frequency of every note.

### Using the samples

Use them freely in your own music and projects. They are raw output of the Furnace
emulators and its bundled instrument presets; the presets were made by many
different people, so if you redistribute the samples themselves, credit
[Furnace](https://github.com/tildearrow/furnace) and check its license and the
original preset authors' terms.

## Regenerating them yourself

```sh
./build-furnace.sh       # build the patched, headless Furnace renderer
./chipruns.py chips      # render chip default voices
./chipruns.py presets    # render installed instrument presets
./chipruns.py --help
```

Requires Python 3, NumPy, Furnace build dependencies, and a C++ toolchain.
`build-furnace.sh` fetches Furnace into `vendor/`; generated files go to `out/`.

The script renders a chromatic run per chip (or per preset, on its native chip),
probes each channel so the audio spans the requested pitches even on chips where
Furnace sounds an octave or two off its note names, and writes a manifest.

## License

The scripts are MIT-licensed. `patches/furnace-batch.patch` modifies Furnace
and is distributed under the upstream GPL-2.0-or-later terms; Furnace source
is fetched from [tildearrow/furnace](https://github.com/tildearrow/furnace).
