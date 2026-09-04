# Citations

Outside work whose **ideas, findings, or data** shaped Emet, and what was taken
from each.

This is not a dependency list — installed packages are declared in each
`pyproject.toml`, and copyright notices live in [NOTICE](NOTICE). This file
exists for the harder-to-track case: a measured result that justifies a
default, a taxonomy that shaped a schema, a phoneme set a data file is written
in. Those leave no trace in a lockfile, and by the time somebody asks where a
constant came from, the reasoning is usually gone.

Each entry records the source, what Emet took, and **the licence of the thing
taken** — because a non-commercially licensed corpus or a differently licensed
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
- Scorer: <https://github.com/SesameAILabs/turnbench> — **MIT**
- Corpus: **non-commercial licence, prohibits voice cloning**

**What Emet took.** Three measured medians from the corpus analysis (§IV-B),
used in [`emet_engine/turn.py`](emet-engine/emet_engine/turn.py) to justify the
default `patience_ms`:

| | |
|---|---|
| Floor transfer offset | −151 ms (listeners begin before the turn ends) |
| Inter-speaker gap | 380 ms |
| Pause within one speaker's turn | 510 ms |

The third against the second is the argument that a silence threshold cannot
separate "still thinking" from "finished", because the two distributions
overlap. Emet's endpointer is deliberately conservative for that reason, and
that reasoning is theirs, not ours.

The paper also supplies the honest grade for what Emet currently ships: an
RMS-energy detector is the benchmark's explicit floor. Recording that is part
of the attribution — the finding was inconvenient, and taking the numbers while
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
<https://github.com/cmusphinx/pocketsphinx> — **BSD-2-Clause (CMU)**

The shipped wake word engine
([`emet_hal/pocketsphinx_wake.py`](emet-hal/emet_hal/pocketsphinx_wake.py)),
used as a dependency rather than copied.

**What is worth naming beyond the dependency**: `SHIPPED_LEXICON` — the
pronunciations that let `emet`, `hugr` and `neuma` be heard — is written in
**ARPAbet**, and is meaningful only against CMU's pronouncing dictionary, which
supplies every other word in a wake phrase. "hey barnaby" needs no lexicon
entry at all because CMUdict already knows the name. That property is the
practical argument for Emet's phonetic-wake design, and it is CMU's work
underneath.

The pronunciations themselves were derived for this project by decoding
reference audio with PocketSphinx's own allphone search, then checked for false
firing.

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
