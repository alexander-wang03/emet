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

Recorded baseline, x86 laptop, 10.1 minutes of real speech through pocketsphinx
and the full turn loop:

```
  frames        7583  (606.6s of audio)
  wakes         58   turns 58
  per frame     mean 1.67 ms   p50 1.08   p95 5.87   max 25.54
  budget        80 ms/frame, 0 frame(s) over
  realtime      0.0209  (48x faster than realtime)
  dropped       0
  verdict       kept up
```

**This is not the number that decides the project.** The reference body is a
Raspberry Pi, and an x86 laptop says nothing about ARM except by comparison.
The same command on a Pi is the measurement that matters, and it has not been
taken. A file replay also exercises everything except the sound card, so
genuine clock drift, where the card's second and the system's slowly diverge,
is still unmeasured and needs a live microphone.
