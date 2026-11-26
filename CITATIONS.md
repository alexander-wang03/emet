# Citations

Outside work whose **ideas, findings, or data** shaped Emet, and what was taken
from each.

This is not a dependency list. Installed packages are declared in each
`pyproject.toml`, and copyright notices live in [NOTICE](NOTICE). This file
exists for the harder-to-track case: a measured result that justifies a
default, a taxonomy that shaped a schema, a phoneme set a data file is written
in. Those leave no trace in a lockfile, and by the time somebody asks where a
constant came from, the reasoning is usually gone.

Each entry records the source, what Emet took, and **the licence of the thing
taken**, because a non-commercially licensed corpus or a differently licensed
repository is a constraint the project has to carry forward.

---

## TurnBench (2026)

**Freeman Jiang, Ramon Sanabria, Soham Deshmukh, Bandhav Veluri, Simon Michael
Vuch Williams, Elliott K. Suen, Garreth Lee, Kevin Yoonho Choi, Takuya Umeki,
Riku Kubo, Sathvik Udupa, Chien-yu Huang, Shih-Yun Shan Kuan, Zhuoyan Tao,
Satyapriya Krishna, Sefik Emre Eskimez, Yu Tsao, Hung-yi Lee, Shinji
Watanabe.** *TurnBench: A Multi-Domain Benchmark for Turn-Taking Dynamics in
Spoken Dialogue.* arXiv:2608.25218 [eess.AS], 25 August 2026.

Sesame AI · Mundo AI · Carnegie Mellon University · National Taiwan University
· Academia Sinica · Oto · Brno University of Technology

- Project: <https://turnbench.sesame.com>
- Scorer: <https://github.com/SesameAILabs/turnbench>, **MIT**
- Corpus: **non-commercial licence, prohibits voice cloning**

**What Emet took.** Three measured medians from the corpus analysis (§IV-B),
used in [`emet_engine/turn.py`](emet-engine/emet_engine/turn.py) to justify the
default `patience_ms`:

| | |
|---|---|
| Floor transfer offset, excluding interruptions | −151 ms (listeners begin before the turn ends) |
| Inter-speaker gap | 380 ms |
| Pause within one speaker's turn | 510 ms |

The third against the second is the argument that a silence threshold cannot
separate "still thinking" from "finished", because the two distributions
overlap. Emet's endpointer is deliberately conservative for that reason, and
that reasoning is theirs, not ours.

The paper also supplies the honest grade for what Emet currently ships: an
RMS-energy detector is the benchmark's explicit floor. Recording that is part
of the attribution: the finding was inconvenient, and taking the numbers while
omitting the verdict would be quoting selectively.

**No corpus data, model weights, or code from this work is redistributed
here.** Only findings are cited. If Emet ever scores itself with their scorer,
the MIT terms apply to the scorer and the non-commercial terms apply to the
corpus, and the distinction has to be respected.

**Their taxonomy is grounded in conversation analysis**, principally Sacks,
Schegloff and Jefferson (1974) on transition-relevance places, and Yngve
(1970) on backchannels. Emet has those concepts second-hand through this paper
rather than from the primary sources, and says so rather than citing work it
has not read.

---

## CMU PocketSphinx and CMUdict

**Carnegie Mellon University Speech Group.** PocketSphinx.
<https://github.com/cmusphinx/pocketsphinx>, **BSD-2-Clause (CMU)**. The
published wheel also carries BSD-3-Clause WebRTC VAD code (Google) and MIT
pieces (a JSON parser, the Python VAD bindings); all permissive.

The shipped wake word engine
([`emet_hal/pocketsphinx_wake.py`](emet-hal/emet_hal/pocketsphinx_wake.py)),
used as a dependency rather than copied.

**What is worth naming beyond the dependency**: `SHIPPED_LEXICON`, the
pronunciations that let `emet`, `hugr` and `neuma` be heard, is written in
**ARPAbet**, and is meaningful only against CMU's pronouncing dictionary, which
supplies every other word in a wake phrase. "hey barnaby" needs no lexicon
entry at all because CMUdict already knows the name. That property is the
practical argument for Emet's phonetic-wake design, and it is CMU's work
underneath.

The pronunciations themselves were derived for this project by decoding
reference audio with PocketSphinx's own allphone search, then checked for false
firing.

`tools/bank/label.py` also uses PocketSphinx's forced alignment and CMUdict to
place each take's words in the voice bank. Its one added word, "octopuses", is
spelled from CMUdict's own "octopus" and "campuses".

---

## Piper, and the LJ Speech voice

**Michael Hansen and the Open Home Foundation.** Piper, a fast local neural
text-to-speech engine. <https://github.com/OHF-Voice/piper1-gpl>,
**GPL-3.0-or-later** (the package `piper-tts`, 1.8.0 on 2026-09-04). The
licence is the GPL because espeak-ng, the phonemiser, is compiled into the
wheel; the earlier `rhasspy/piper` was MIT and is no longer where releases
come from. Depends on ONNX Runtime (Microsoft, **MIT**).

The shipped local voice
([`emet_providers/piper.py`](emet-providers/emet_providers/piper.py)), used as
a dependency behind the optional extra `emet-providers[piper]`. Emet imports
it and does not copy, bundle or redistribute it, so the Apache-2.0 terms of
this repository are unaffected and a body installed without the extra
carries no GPL code. Anyone packaging Emet *with* Piper inside one
distribution takes on the GPL's terms for that distribution, and should know
it. Facts verified against PyPI and the repository on 2026-09-15.

**Keith Ito and Linda Johnson.** The LJ Speech Dataset.
<https://keithito.com/LJ-Speech-Dataset/>, **public domain**. The reference
soul's voice, `en_US-ljspeech-medium`, is a Piper model trained on it
(model card in `rhasspy/piper-voices` on Hugging Face, read 2026-09-15). It
was chosen over Amy, the earlier reference voice, because Amy's model card
says it is fine-tuned from the Lessac voice, and the Blizzard 2013 Lessac
corpus licence excludes "the development, marketing, commercialisation, sale
or licencing of voice synthesis" products; a default a project promises
people may fork and sell cannot rest on that. The voice files themselves are
downloaded by the owner and are not in this repository.

## faster-whisper and Whisper small.en

**SYSTRAN.** faster-whisper 1.2.1, <https://github.com/SYSTRAN/faster-whisper>,
**MIT**. The model is `Systran/faster-whisper-small.en` at revision d1d751a5,
**MIT** on its card, a CTranslate2 conversion of OpenAI's Whisper small.en
(<https://github.com/openai/whisper>, **MIT**; the Hugging Face card for
`openai/whisper-small.en` says Apache-2.0, so the two sources disagree, both
read 2026-10-27 and 28). A tool: `tools/bank/label.py`
runs it in a tools venv to write a transcript of each take of the private
voice bank, and none of the four packages imports it. Nothing is copied.

## The Reachy LiteRT sample's environment record

**The Google AI Edge Authors.** `samples/litert_lm/reachy-voice-robot/emulator/env.py`
in <https://github.com/google-ai-edge/litert-samples> at 99fd82f
(2026-10-24), **Apache-2.0** (the repository's LICENSE and the sample's
README, verified 2026-10-27). The method behind the board lines in the
`--stats` footer
([`emet_engine/board.py`](emet-engine/emet_engine/board.py)):
`vcgencmd measure_temp` and `vcgencmd get_throttled` parsed as text, with
the load average, before and after a run. A port of its lines 107 to 151
and 174 to 192, rewritten in Emet's style; the file keeps its copyright
line. The identity line's `not-installed` for a missing package is its
line 41's. Left out: the hostname and the kernel command line, which a
footer pasted into a public pull request should not carry. Neither the
repository's root nor the sample has a NOTICE file.

## Pollen Robotics: microduck and reachy_mini

**Pollen Robotics.** <https://github.com/pollen-robotics/microduck> at
942e58e and <https://github.com/pollen-robotics/reachy_mini> at 9fb2bfcbd,
both **Apache-2.0** (verified 2026-10-27). Two ideas, and one CI step
adapted:

- Each microduck daemon's first log line is its identity, build and
  revision (`docs/design/architecture.md` lines 531 to 536 and 591 to
  592). Emet's identity line at boot
  ([`emet_engine/cli.py`](emet-engine/emet_engine/cli.py)) asks git at
  boot where microduck compiles the revision in, and says the revision is
  unknown for an installed copy.
- microduck's CI runs its arm64 work on GitHub's `ubuntu-24.04-arm`
  runner (`.github/workflows/ci.yml`). Emet's advisory arm64 job does the
  same.
- reachy_mini's CI runs its audio code against a PulseAudio null sink
  looped to capture. The setup lines of Emet's `virtual sound card` job
  are adapted from its `.github/actions/virtual-sound-card/action.yml`,
  with names changed: the same `pulseaudio` command, `~/.asoundrc` and
  `PULSE_SERVER` line. The real PortAudio path then runs with no sound
  card.

## VCR and vcrpy

**Ruby's VCR** (<https://github.com/vcr/vcr>, **Hippocratic License 2.1**)
and **Python's vcrpy** (<https://github.com/kevin1024/vcrpy>, **MIT**),
both checked 2026-10-27. The idea and the name of a cassette, an exchange
recorded once and replayed when the same request comes again, behind the
mock transcriber's cassette mode
([`emet_providers/cassette.py`](emet-providers/emet_providers/cassette.py)).
Emet keys a turn by the sha256 of the exact bytes fed rather than by an
HTTP request. No code is taken from either, and neither is a dependency.

---

## Adding to this file

If a change takes an idea, a finding, a number, or a data format from outside
work, add it here and cite it at the point of use. See
[CONTRIBUTING.md](CONTRIBUTING.md#citing-outside-work). Two rules that are easy
to get wrong:

- **Ideas count, not only code.** Using a paper's measurement to pick a
  constant is taking something, even though nothing was copied.
- **Record the licence of what was taken**, not just of the repository it came
  from. A project can ship an MIT tool alongside a corpus you may not use
  commercially, and only one of those is safe to build on.
