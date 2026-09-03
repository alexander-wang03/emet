# emet-engine

The listen loop and the runtime that drives a body.

Imports `emet_sdk` only. Hardware reaches it by name through entry-point
discovery, never by import — see the layering check in `tools/check_layering.py`.

```sh
emet-listen path/to/manifest.yaml path/to/soul.yaml
emet-listen path/to/manifest.yaml path/to/soul.yaml --replay recording.wav
```

Needs `emet-hal[audio,wake]` installed alongside for a microphone and a
detector to exist.
