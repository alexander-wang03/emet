# emet-engine

The listen loop and the runtime that drives a body.

Imports `emet_sdk` only. Hardware reaches it by name through entry-point
discovery, never by import. See the layering check in `tools/check_layering.py`.

```sh
emet-listen path/to/manifest.yaml path/to/soul.yaml
emet-listen path/to/manifest.yaml path/to/soul.yaml --replay recording.wav
```

Needs `emet-hal[audio,wake]` installed alongside for a microphone and a
detector to exist.

## Checking that it keeps up

The 0.3 acceptance bar is ten minutes without drift, which is a number rather
than a feeling. `--stats` produces it:

```sh
emet-listen manifest.yaml soul.yaml --replay ten-minutes.wav --stats
```

The figure that matters is **realtime**: processing time divided by the audio
processed. Audio arrives at a fixed rate whatever the CPU is doing, so 1.0 is
exactly keeping up with no margin and anything above it is falling behind.
`frames over budget` matters separately: a loop with a fine average that
overruns once a minute is dropping a word once a minute, and the average hides
it.

A run is only evidence if nothing was dropped, nothing went over budget, and
there is real margin. A bare pass on an idle machine is not a pass on a busy
one.

Recorded on both platforms from the same ten-minute recording, made on the
reference body with `tools/soak_prompts.sh` (49 wake phrases, 9 decoys):

```
                Raspberry Pi 5               x86 laptop
  frames        7500  (600.0s of audio)      7500
  per frame     mean 2.00 ms  p95 2.99       mean 1.89 ms  p95 3.18
                max 7.88                     max 9.86
  budget        80 ms/frame, 0 over          0 over
  realtime      0.0250  (40x)                0.0237  (42x)
  dropped       0                            0
  verdict       kept up                      kept up
```

A file replay exercises everything except the sound card. A live run adds a
`clock` line, the wall clock against the audio clock, and a card-overflow
count beside `dropped`. The reference body's live figures are in the 0.3
release notes.
