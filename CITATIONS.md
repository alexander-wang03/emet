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
that reasoning is theirs, not ours. [`DESIGN.md`](DESIGN.md) section 13's turn
budget quotes the inter-speaker gap, 380 ms, as a horizon and never a target.

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

## Shiwa et al., HRI 2008

**Shiwa, Kanda, Imai, Ishiguro and Hagita.** *How quickly should
communication robots respond?* HRI 2008, doi:10.1145/1349822.1349843. Two
findings of the abstract (OpenAlex, verified 2026-11-25; the paper not
read): "user preference for SRT is the highest at
one second, and ratings level off at two seconds", and a filler softened a
long wait. Numbers only, no text beyond the quotes and no code.
[`DESIGN.md`](DESIGN.md) section 13 takes them as the turn budget, 2.0 s at
the median and 1.0 s as the stretch, and
[`tools/body/run.py`](tools/body/run.py) measures the row. Reading "level off"
as a limit is this project's reading.

## Stivers et al., PNAS 2009

**Stivers et al.** *Universals and cultural variation in turn-taking in
conversation.* PNAS 2009, doi:10.1073/pnas.0903616106. One finding of the
abstract (OpenAlex, verified 2026-11-25): across ten languages the mean gaps
fall "within a range of 250 ms". [`DESIGN.md`](DESIGN.md) section 13 quotes it
with TurnBench's 380 ms as the horizon, never as a target. Numbers only.

## PARADISE

**Walker, Litman, Kamm and Abella.** *PARADISE: A Framework for Evaluating
Spoken Dialogue Agents.* ACL-EACL 1997, <https://aclanthology.org/P97-1035/>.
An idea, read in the paper on
2026-11-25: a person's satisfaction modelled from task success and the
dialogue's measured costs. Emet puts the maintainer's rating of each turn
beside that turn's timings in `sitting.json`
([`tools/body/run.py`](tools/body/run.py), EV4 stage 1). No text or code; the
paper's licence not read (unverified).

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
both **Apache-2.0** (verified 2026-10-27, and microduck's again through
the GitHub API on 2026-11-26). Four ideas, and one CI step adapted:

- Each microduck daemon's first log line is its identity, build and
  revision (`docs/design/architecture.md` lines 531 to 536 and 591 to
  592). Emet's identity line at boot
  ([`emet_engine/cli.py`](emet-engine/emet_engine/cli.py)) asks git at
  boot where microduck compiles the revision in, and says the revision is
  unknown for an installed copy.
- microduck's CI runs its arm64 work on GitHub's `ubuntu-24.04-arm`
  runner (`.github/workflows/ci.yml`). Emet's advisory arm64 job does the
  same.
- microduck's `board` job runs `scripts/board-test.sh`, the script a
  person also runs on their own machine (`.github/workflows/ci.yml` line
  237 at 942e58e, and the script's usage line, read 2026-11-26). Emet's
  [`tools/ci.py`](tools/ci.py) holds every CI job's steps in one script
  that GitHub, the laptop and the body runner call. The idea is taken; no
  code is.
- microduck's `robotctl monitor` draws the running robot's state in place
  at a terminal, and a reading not yet taken says so (`batt not read yet`;
  `docs/robot/cheatsheet.md`, "Watching the loop", lines 35 to 105 at
  942e58e, read 2026-11-27). The live line under `--stats`
  ([`emet_engine/live.py`](emet-engine/emet_engine/live.py)) takes that
  idea for the ears, with `no wake yet` for a wake not yet heard. The
  monitor prints one line per tick when piped; Emet's line prints nothing
  off a terminal. No code is taken.
- reachy_mini's CI runs its audio code against a PulseAudio null sink
  looped to capture. The setup lines of Emet's `virtual sound card` job
  are adapted from its `.github/actions/virtual-sound-card/action.yml`,
  with names changed: the same `pulseaudio` command, `~/.asoundrc` and
  `PULSE_SERVER` line. The real PortAudio path then runs with no sound
  card. The same null sink carries the job's live run (CI6), in which
  `emet-listen` hears the generated clip played into it. The live run is
  Emet's own.

## VCR and vcrpy

**Ruby's VCR** (<https://github.com/vcr/vcr>, **Hippocratic License 2.1**)
and **Python's vcrpy** (<https://github.com/kevin1024/vcrpy>, **MIT**),
both checked 2026-10-27. The idea and the name of a cassette, an exchange
recorded once and replayed when the same request comes again, behind the
mock transcriber's cassette mode
([`emet_providers/cassette.py`](emet-providers/emet_providers/cassette.py)).
Emet keys a turn by the sha256 of the exact bytes fed rather than by an
HTTP request. No code is taken from either, and neither is a dependency.

## certifi

**Kenneth Reitz and the certifi maintainers.** certifi, Mozilla's bundle of
trusted root certificates for Python. <https://github.com/certifi/python-certifi>,
**MPL-2.0** (2026.10.18 on PyPI, checked 2026-11-26). It is here because
MPL-2.0 is outside the licence gate's allowed list, and the maintainer
decided on 2026-11-26 that an MPL-2.0 dependency taken unmodified and never
bundled is allowed, named here and in the gate's exceptions. The list and
the exceptions are both in `tools/licences.json`; the gate reads that file
alone, so nothing yet checks that an exception has its entry here. `httpx`
brings certifi in the `deepgram`, `anthropic` and `openai` extras of
`emet-providers`, which also floor it at 2026.10.18, the first version past
advisory PYSEC-2026-4320. Emet never imports it, copies it or ships it:
MPL-2.0's terms reach only certifi's own files, and an install carries them
unchanged.

## Release gates from other projects' release processes

Four written release processes, read by the survey of 2026-11-25 and again
on 2026-11-26. Ideas only: no text and no code is taken from any of them,
and none is a dependency. They stand behind the two gates
[`tools/release_audit.py`](tools/release_audit.py) holds on the merge day.

- **PX4 Autopilot**, the release process in the PX4 User Guide,
  <https://docs.px4.io/main/en/releases/release_process.html>. The guide is
  **CC BY 4.0** (<https://docs.px4.io/main/en/contribute/licenses.html>). Its
  alpha stage ends when every test card passes across a hardware matrix with
  no critical or high-severity failure. Emet refuses a merge until the
  reference body has passed a run of the head, or of a commit that differs
  from it only in `CHANGELOG.md` ([RELEASING.md](RELEASING.md) has the
  whole rule).
- **Python**, PEP 101, <https://peps.python.org/pep-0101/>, **public domain**
  by its own Copyright section. No release is made with an open release
  blocker.
- **The Zephyr Project**, its release process,
  <https://docs.zephyrproject.org/latest/project/release_process.html>. The
  project is **Apache-2.0** by its licensing page, which names no separate
  licence for the documentation. Every blocker bug is resolved before a
  release is created.
- **The Rust Forge**, compiler prioritization,
  <https://forge.rust-lang.org/compiler/prioritization.html>, in a repository
  under **MIT or Apache-2.0** (<https://github.com/rust-lang/rust-forge>).
  `P-critical` marks an issue that may block a release.

From the three defect rules Emet takes one idea: a finding of the top
severity that is still open refuses every merge, and one of the next
severity refuses a minor's or a major's. Each page was read on 2026-11-26.

The scale itself, S1 to S4, is Emet's, after the triage rules of
Everchronicle, the maintainer's other project. Four rules of the ledger in
item 10 of [RELEASING.md](RELEASING.md) are taken from that project's
release process as ideas: reproduce a finding first; give it a regression
test, or a written reason why there is none; log it even when it is fixed
at once; never delete a row. That process is the maintainer's own work,
under no published licence (its repository has no licence file, read
2026-11-26); only its ideas are taken, and no text is copied.

## EasyNavigation

**The EasyNavigation project.** <https://github.com/EasyNavigation/EasyNavigation>
at 86ea9a3, **Apache-2.0** (its `LICENSE` at that commit, read through the
GitHub API, verified 2026-11-26). One idea, no code. Its roadmap item RD032
(`ROADMAP.md` line 58) lists a "configuration fingerprint" among the parts
of a safety mode. The `body` field of the body runner
([`tools/body/run.py`](tools/body/run.py)) is that idea for a robot's
body: the sha256 of the body manifest in canonical form and of the
body-local state file, in every `run.json`, so a run's evidence names the
body it ran on. The canonical form and the code are Emet's.

## Reproducible Builds

**The Reproducible Builds project.** Its definition of a reproducible
build, <https://reproducible-builds.org/docs/definition/>, **CC BY-SA 4.0**
(the site's footer, verified 2026-11-26): the same source code, build
environment and build instructions give bit-for-bit identical artifacts.
The wheel job of [`tools/ci.py`](tools/ci.py) builds each wheel twice from
one commit and compares the hashes. An idea; no text and no code.

## zizmor

**William Woodruff and the zizmor contributors.** zizmor, static analysis
for GitHub Actions, <https://github.com/zizmorcore/zizmor>, **MIT** (PyPI's
JSON for 1.32.0 and the repository's `LICENSE` at v1.32.0, verified
2026-11-26). A CI-only tool, pinned at 1.32.0 in
`tools/ci/requirements.txt` and never imported by the four packages. One
number is taken beside the tool: seven days, the cooldown zizmor
recommends before Dependabot proposes a release (`docs/audits.md` at
v1.32.0, its `dependabot-cooldown` audit, read 2026-11-26). It is kept in
[`.github/dependabot.yml`](.github/dependabot.yml),
[`tools/ci/zizmor.yml`](tools/ci/zizmor.yml) and
[`tools/ci/requirements.in`](tools/ci/requirements.in).

## OpenSSF Scorecard

**The Open Source Security Foundation.** `ossf/scorecard-action`,
<https://github.com/ossf/scorecard-action>, **Apache-2.0** (its `LICENSE`
at v2.6.0, verified 2026-11-26). Run weekly by
[`.github/workflows/weekly.yml`](.github/workflows/weekly.yml), pinned to
23f22cd, the commit the tag v2.6.0 points to. The job's permissions, its
steps and its default-branch condition follow the action's README and its
example workflow, `examples/scorecard.yml`, both at v2.6.0. No code is
copied into the repository.

## The security process and the checks that hold it

Ideas and identifier formats, the GHSA and CVE forms as the regular
expressions their sources publish. No other text or code is taken from any
of these, and none is a dependency. Each page and each licence was read on
2026-11-26, a repository's licence from its licence file through the
GitHub API.

- **The Zephyr Project**, its vulnerability reporting process,
  <https://docs.zephyrproject.org/latest/security/reporting.html>. The
  page carries a copyright notice for the Zephyr Project members and
  individual contributors and gives no licence of its own; the project's
  repository is **Apache-2.0**. [`SECURITY.md`](SECURITY.md) takes three
  ideas from it: an embargo of at most 90 days, a CVE request for each
  confirmed vulnerability, and the reporter credited unless they ask not to
  be. Its first response within a week matches the week `SECURITY.md`
  promised before the page was read.
- **Olivier Lacan.** Keep a Changelog 1.1.0,
  <https://keepachangelog.com/en/1.1.0/>, **MIT** (the site and its
  repository's `LICENSE`). The changelog follows its format, and its
  `Security` section, "in case of vulnerabilities", is the one
  [`tools/release_check.py`](tools/release_check.py) holds to an advisory
  id.
- **GitHub Docs**, "GitHub Advisory database",
  <https://docs.github.com/en/code-security/concepts/vulnerability-reporting-and-management/github-advisory-database>,
  **CC BY 4.0** (the `github/docs` README licenses its content folder so).
  The form of a GHSA id, three groups of four from `23456789cfghjmpqrvwx`,
  and the page's regular expression for it, written with a non-capturing
  group in `tools/release_check.py`.
- **The CVE record format**, <https://github.com/CVEProject/cve-schema>,
  **CC0-1.0**. The `cveId` pattern of `schema/CVE_Record_Format.json`,
  `CVE-`, four digits and four to nineteen digits, in
  `tools/release_check.py`.
- **The OSV schema**, <https://github.com/ossf/osv-schema>, **Apache-2.0**.
  The `PYSEC` prefix of the PyPI advisory database, from the table of
  prefixes in its `docs/schema.md`, in `tools/release_check.py`.
- **LeRobot**'s pre-commit configuration, which runs **gitleaks**
  (<https://github.com/huggingface/lerobot>, **Apache-2.0**;
  <https://github.com/gitleaks/gitleaks>, **MIT**). The idea of scanning
  the tree for what must never be pushed, in
  [`tools/check_style.py`](tools/check_style.py)'s address rule. No rule or
  code of either is used.
- **D. Eastlake and A. Panitz.** RFC 2606, Reserved Top Level DNS Names,
  BCP 32, June 1999, <https://www.rfc-editor.org/rfc/rfc2606.html>.
  Copyright The Internet Society (1999), whose statement lets the document
  be copied and published with its notice kept. `example.com` (section 3)
  and `.invalid` (section 2) are the placeholder domains
  `tools/check_style.py` allows. Only the reserved names are taken.
- **P. Resnick, ed.** RFC 5322, Internet Message Format, October 2008,
  <https://www.rfc-editor.org/rfc/rfc5322.txt>. Copyright The IETF Trust
  (2008), subject to the rights, licenses and restrictions of BCP 78 (its
  Full Copyright Statement, read 2026-11-26). Section 3.2.3's `atext`, the
  characters an address's local part may hold: `tools/check_style.py` lets
  a local part end in one of them. Only the character set is taken.
- **John MacFarlane.** CommonMark Spec 0.31.2,
  <https://spec.commonmark.org/0.31.2/>, **CC BY-SA 4.0** (the page's own
  notice, read 2026-11-26). Section 4.2 (up to three spaces before a
  heading's `#`), section 5.2 (an ordered mark is one to nine digits and
  `.` or `)`, and an ordered list that interrupts a paragraph starts at 1)
  and section 5.3 (a changed delimiter starts a new list), in
  `tools/release_check.py`'s Security reader. Only the rules are taken.

## Semantic Versioning and REP 2004

Ideas only: Emet copies no text and no code from either. Both were read on
2026-11-26.

- **Tom Preston-Werner.** Semantic Versioning 2.0.0,
  <https://semver.org/spec/v2.0.0.html>, **CC BY 3.0** (the page's licence
  line). Its item 1: "Software using Semantic Versioning MUST declare a
  public API."
- **William Woodall.** REP 2004, Package Quality Categories,
  <https://github.com/ros-infrastructure/rep/blob/master/rep-2004.rst>,
  **public domain or CC0-1.0**, whichever is more permissive, by its own
  Copyright section. The repository is archived. Two of its requirements
  for a Level 1 package: item 1.iii, "Must have a strictly declared public
  API", and item 4.iv.b, "Must have a performance regression policy".

The two public-API rules stand behind
[`emet-sdk/tests/test_public_api.py`](emet-sdk/tests/test_public_api.py),
which holds the SDK's public API equal to a golden file and cites them.
They stand behind [`tools/api_diff.py`](tools/api_diff.py) as well, which
prints that API against master; its docstring does not cite them yet
(read 2026-11-26). REP 2004's regression policy, with airspeed velocity
below, stands behind the stated bound of the body runner's budgets in
[`tools/body/run.py`](tools/body/run.py): a measure may rise by no more
than the larger of 25% and the spread its repeated runs show.

## airspeed velocity

**The airspeed velocity developers.** asv, a tool for benchmarking Python
packages over their lifetime, <https://github.com/airspeed-velocity/asv>,
**BSD-3-Clause** (its `LICENSE.rst` through the GitHub API, verified
2026-11-26). One idea, no code, and no dependency: a benchmark history held
release against release. The body runner's budgets in
[`tools/body/run.py`](tools/body/run.py) hold each release's numbers to the
last one's on the same machine.

## He et al., MSR 2026

**Hao He, Courtney Miller, Shyam Agarwal, Christian Kästner and Bogdan
Vasilescu.** *Speed at the Cost of Quality: How Cursor AI Increases
Short-Term Velocity and Long-Term Complexity in Open-Source Projects.*
MSR '26, Rio de Janeiro, pages 181 to 193, doi:10.1145/3793302.3793349,
arXiv:2511.04427, **CC BY 4.0** (arXiv's page and Crossref's record,
verified 2026-11-26). Its finding: adopting the agent brought a persistent
rise in static-analysis warnings and code complexity. That finding is the
idea behind the budget in [`tools/check_health.py`](tools/check_health.py),
which may only fall. No number and no text is taken.

## Licence, document and setup checks from other projects

Ideas only: no code is taken from any of them, and none is a dependency.
Each was read on 2026-11-26, its licence from the repository's licence
file through the GitHub API.

- **OpenVoiceOS**, the reusable `license-check.yml` workflow in
  <https://github.com/OpenVoiceOS/gh-automations> (`dev` branch),
  **Apache-2.0**. It fails a run whose installed dependencies carry a
  strong or network copyleft licence, and by default a weak one; it allows
  MPL-2.0 by default, takes named package exceptions, and reads the
  licences with `pip-licenses`.
  [`tools/check_licences.py`](tools/check_licences.py) holds Emet's
  dependencies to [`tools/licences.json`](tools/licences.json) the same
  way, reading the metadata with the standard library.
- **PX4 Autopilot**, `.github/workflows/docs-orchestrator.yml` in
  <https://github.com/PX4/PX4-Autopilot>, **BSD-3-Clause**: a link check on
  every pull request that changes its documents.
  [`tools/check_docs.py`](tools/check_docs.py) checks Emet's links and runs
  the documents' safe commands.
- **github-slugger**, <https://github.com/Flet/github-slugger>, **ISC**: the
  rule GitHub uses to make a heading's anchor, which `check_docs.py`
  follows to check a link's `#fragment`.
- **GitHub's Scripts to Rule Them All**,
  <https://github.com/github/scripts-to-rule-them-all>, **CC0-1.0**,
  archived: a `script/setup` for a fresh clone and a `script/update` after
  a pull. [`tools/dev.py`](tools/dev.py) takes the two names.
- **Home Assistant**, `script/setup` in
  <https://github.com/home-assistant/core> (`dev` branch), **Apache-2.0**:
  it makes `.venv` and installs into it, as `tools/dev.py setup` makes
  `emet-sdk/.venv`.

## pip-audit

**The Python Packaging Authority.** pip-audit 2.10.1,
<https://github.com/pypa/pip-audit>, **Apache-2.0** (PyPI's JSON for
2.10.1, its classifier, and the repository's `LICENSE`, verified
2026-11-26). A tool in two places. [`tools/ci.py`](tools/ci.py) runs it in
the weekly `pip-audit (advisory)` job, pinned at 2.10.1 by hash in
[`tools/ci/requirements.txt`](tools/ci/requirements.txt). The body runner,
[`tools/body/run.py`](tools/body/run.py), installs it on the reference body
in a venv of its own from the same lock's hashed pins, with
`--require-hashes`, and audits the runner's venv. The four packages never
import it.

## mutmut and vulture

Two CI-only tools, each pinned by hash in
[`tools/ci/requirements.txt`](tools/ci/requirements.txt) and never imported
by the four packages. Licences from PyPI's JSON, verified 2026-11-26.

- **Anders Hovmöller and the mutmut contributors.** mutmut 3.8.1,
  <https://github.com/boxed/mutmut>, **BSD-3-Clause**. The
  `mutmut (advisory)` job of [`tools/ci.py`](tools/ci.py) runs it, and the
  script reads mutmut's own formats as 3.8.1 writes them: the letter that
  joins a class to its method in a mutant's name (`format_utils.py`), the
  `x_` mangling and the `not checked` status (`__main__.py` and
  `stats.py`), and the `mutants/` folder's layout
  (`utils/file_utils.py`). Each is cited where `tools/ci.py` uses it; no
  code is copied.
- **Jendrik Seipp and the vulture contributors.** vulture 2.16,
  <https://github.com/jendrikseipp/vulture>, **MIT**.
  [`tools/check_health.py`](tools/check_health.py) runs it for the unused
  names and reads its output line, `path:line: what (N% confidence)`. No
  code is copied.

## nftables element timeouts

**The nftables wiki**, "Element timeouts",
<https://wiki.nftables.org/wiki-nftables/index.php/Element_timeouts>,
**GFDL 1.3 or later** (the page's footer, verified 2026-11-26). One idea,
no code: a set element with a timeout is deleted when its countdown
reaches zero. [`tools/body/netcut.sh`](tools/body/netcut.sh) bounds its
cut with such an element, so a script killed outright cannot leave the
body cut. The script calls the `nft` program Raspberry Pi OS ships.

## The generated bank's voices

[`tools/bank/generate.py`](tools/bank/generate.py) speaks with four Piper
voices to make test audio, through Piper run as a tool (Piper's entry
above). The voices come from `rhasspy/piper-voices` on Hugging Face at
revision 5ec43b18 (the repository is tagged MIT; each model card names its
dataset's licence). Each is listed in piper1-gpl's `docs/COMMERCIAL_USE.md`
at commit badc920, the file's sha256 c3e7bf3c. Each card and the list were
read and hashed on 2026-11-26. The voices are downloaded by whoever builds
the bank and are not in this repository. The bank's `ATTRIBUTION.md`, which
`generate.py` writes, credits each, with any term a dataset adds to its
licence, as for Alba below.

- `en_US-ljspeech-medium`: the LJ Speech Dataset, above, **public domain**.
- `en_US-norman-medium`: trained from scratch by Bryce Beattie on
  recordings from LibriVox (<https://librivox.org>), **public domain** by
  its card and by LibriVox's own page on its recordings.
- `en_GB-alba-medium`: the Alba speech corpus, Cassia Valentini-Botinhao
  and Junichi Yamagishi, University of Edinburgh, 2019,
  <https://doi.org/10.7488/ds/2506>, **CC BY 4.0** by its DataCite record,
  which adds that any use derogatory to the voice talent is prohibited.
  The bank's own records carry that term too:
  `tools/bank/generated/voices.json` records it under `terms` for g03, from
  the corpus's licence file
  (<https://datashare.ed.ac.uk/bitstream/handle/10283/3270/license_text.txt>,
  read 2026-11-26), and each set's `ATTRIBUTION.md` and `DATASHEET.md`
  quote it. Fine-tuned from `en_US-hifi_tts_92-medium`, trained on the Hi-Fi
  Multi-Speaker English TTS Dataset (Evelina Bakhturina, Vitaly Lavrukhin,
  Boris Ginsburg and Yang Zhang, Interspeech 2021,
  doi:10.21437/interspeech.2021-1599; <https://www.openslr.org/109/>),
  **CC BY 4.0** by its OpenSLR page.
- `en_US-joe-medium`: recordings from
  <https://github.com/OHF-Voice/voice-datasets>, **CC0** by its README;
  fine-tuned from `nl_BE-rdh-medium`, trained on dutch-vl-tts
  (<https://github.com/r-dh/dutch-vl-tts>), **CC0-1.0** by its `LICENSE`.

The clip under `emet-hal/tests/clips/` is `en_US-ljspeech-medium` saying
the wake phrase, and its record names the voice's hashes. On whether the
GPL reaches audio Piper makes: the GPL FAQ
(<https://www.gnu.org/licenses/gpl-faq.html#GPLOutput>, **CC BY-ND 4.0**,
verified 2026-11-26) says the output of a program that translates its
input takes the copyright status of that input.

## The generated bank's rooms and noise

Ideas only, and the constants of one public-domain program. Each is cited
in [`tools/bank/generate.py`](tools/bank/generate.py): Allen and Berkley,
Schroeder and Gebru et al. at the code that uses them, and Gardner and
SplitMix64 in the module's docstring, where the noise is described. The
records were read through Crossref and arXiv, and Vigna's file from its
page, on 2026-11-26. The four papers with a DOI were not opened, so what
each says beyond its title is unverified here. Each paper is under its
publisher's copyright: Crossref records no licence for the three oldest,
and ACM's copyright policy for the fourth.

- **Jont B. Allen and David A. Berkley**, "Image method for efficiently
  simulating small-room acoustics", J. Acoust. Soc. Am. 65(4), 943 to 950,
  1979, doi:10.1121/1.382599. The rooms: each mirror image of the talker
  in a box room is one tap.
- **M. R. Schroeder**, "New Method of Measuring Reverberation Time",
  J. Acoust. Soc. Am. 37(3), 409 to 412, 1965, doi:10.1121/1.1909343. The
  decay each manifest records, read from the response by backward
  integration.
- **Martin Gardner**, "Mathematical Games", Scientific American 238(4), 16
  to 32, April 1978, doi:10.1038/scientificamerican0478-16, the column
  that describes Richard Voss's algorithm for pink noise: rows of white
  noise, row k held for 2**k samples, summed.
- **Guy L. Steele Jr., Doug Lea and Christine H. Flood**, "Fast splittable
  pseudorandom number generators", OOPSLA 2014, 453 to 472,
  doi:10.1145/2660193.2660195; and **Sebastiano Vigna**'s `splitmix64.c`
  (2015, <https://prng.di.unimi.it/splitmix64.c>), dedicated to the
  **public domain** by its header. The noise's values come from
  SplitMix64's mixing function, whose three constants and three shifts are
  taken from that code.
- **Timnit Gebru, Jamie Morgenstern, Briana Vecchione et al.**, "Datasheets
  for Datasets", 2018, arXiv:1803.09010, under arXiv's non-exclusive
  licence to distribute. The questions each set's `DATASHEET.md` answers,
  an idea its abstract states: a datasheet that documents a dataset's
  motivation, composition, collection process and recommended uses.

## PEP 503

**Donald Stufft.** PEP 503, Simple Repository API, 2015,
<https://peps.python.org/pep-0503/>, **public domain** by its own Copyright
section, "This document has been placed in the public domain" (read
2026-11-27). Its name normalisation, an idea and its one regular
expression: a name is lower-cased and each run of `.`, `-` or `_` becomes
one `-`, `re.sub(r"[-_.]+", "-", name).lower()`.
[`emet_sdk/discovery.py`](emet-sdk/emet_sdk/discovery.py)'s `_claimant`
compares the distributions behind two entry points by that name, so one
distribution written two ways is never refused as a clash with itself.

## OpenAI's API documentation

**OpenAI.** Pages at <https://developers.openai.com/api/>, read 2026-11-27:
the Chat Completions reference (`reasoning_effort`, "Currently supported
values are `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, and `max`";
`max_completion_tokens` counts "visible output tokens and reasoning
tokens"); the model page of gpt-5.6-terra (none to max, `medium` the
default); the reasoning guide (a cap can be spent "before any visible output
tokens are produced"); the guide to migrating to Responses (from GPT-5.4,
no tool calls over Chat Completions at an effort other than `none`); the
Models API's retrieve (`shutdown_date`, a date or null); and the
error-codes guide (four billing codes of a 429, with `insufficient_quota`
as their type, and "Retrying billing, spend, or quota errors won't restore
API access"). Facts about an interface, taken as facts under OpenAI's terms
of use; no text or code is copied. Used in
[`emet_providers/openai.py`](emet-providers/emet_providers/openai.py)
(`FAULT_CODES`, the preflight, `reasoning_effort`, the empty reply at the
cap) and its tests.

## Ollama, llama.cpp and litert-lm, for the local recipes

**Ollama** (**MIT**): its FAQ and its OpenAI compatibility page at
<https://docs.ollama.com/>, read 2026-11-27: it binds 127.0.0.1:11434,
unloads a model after 5 idle minutes unless `OLLAMA_KEEP_ALIVE` is negative,
uses 4096 tokens of context unless `OLLAMA_CONTEXT_LENGTH` says otherwise,
documents `max_tokens`, reads `reasoning_effort`, ignores the key, and
preloads a model on "an empty request", the idea behind `params.warm_up`.
**llama.cpp** (ggml-org, **MIT**): `tools/server/README.md` on master, read
2026-11-27: `--host` 127.0.0.1 and `--port` 9931 by default, `cache_prompt`
on by default, `reasoning_effort` `none` turns thinking off.
**litert-lm** (Google AI Edge, **Apache-2.0**) 0.18.1, as run 2026-10-27:
`serve` reads `max_completion_tokens` and checks no key. Facts for
[`emet-providers/README.md`](emet-providers/README.md) and the warm-up's
docstring; no code is taken.

## Vendor documentation behind the model lifecycle and the failure kinds

Facts and wire formats read at the vendors' own pages, used in
[`emet_providers/anthropic.py`](emet-providers/emet_providers/anthropic.py)
and [`emet_providers/lifecycle.py`](emet-providers/emet_providers/lifecycle.py).
No code and no text beyond short phrases quoted in comments. The pages
state no open licence (unverified); what Emet takes from them are facts.

- **Anthropic**, API versions, <https://platform.claude.com/docs/en/api/versioning>,
  read 2026-11-27: what 2026-10-21 refuses, and that Anthropic recommends
  the latest version. `API_VERSION`.
- **Anthropic**, Get a Model, <https://platform.claude.com/docs/en/api/models/retrieve>,
  read 2026-11-27: `lifecycle`, `deprecated_at`, `retires_at`. The preflight.
- **Anthropic**, Claude API errors and its `ErrorDetails` list,
  <https://platform.claude.com/docs/en/api/errors>, read 2026-11-27: 402
  `billing_error` and the 429 spend-cap codes. `FAULT_CODES`.
- **Anthropic**, Streaming messages, <https://platform.claude.com/docs/en/build-with-claude/streaming>,
  read 2026-11-27: `message_delta`'s usage is cumulative. The token counts.
- **Anthropic**, Model deprecations, <https://platform.claude.com/docs/en/about-claude/model-deprecations>,
  read 2026-11-27: claude-opus-5 not retired before 2027-07-24, and a
  retired model's request answered 404 `not_found_error`.
- **OpenAI**, Deprecations, <https://developers.openai.com/api/docs/deprecations>,
  read 2026-11-27: gpt-5.6-terra only a replacement; six months' notice
  for a generally available model. Its lifecycle row.
- **Deepgram**, <https://developers.deepgram.com/docs/models-languages-overview>
  and <https://developers.deepgram.com/docs/tts-models>, read 2026-11-27:
  nova-3 and aura-2-thalia-en listed, no retirement stated. Their rows.

## Deepgram's error pages

**Deepgram.** API errors, <https://developers.deepgram.com/docs/errors> and
<https://developers.deepgram.com/reference/errors>, and STT troubleshooting
for WebSocket, NET and DATA errors,
<https://developers.deepgram.com/docs/stt-troubleshooting-websocket-data-and-net-errors>,
all read 2026-11-27. The statuses and `err_code`s behind the Deepgram
transcriber's and voice's `FAULT_CODES`
([`deepgram.py`](emet-providers/emet_providers/deepgram.py),
[`deepgram_voice.py`](emet-providers/emet_providers/deepgram_voice.py)):
402 `ASR_PAYMENT_REQUIRED` as out of credit, and 403
`INSUFFICIENT_PERMISSIONS` as a model the project cannot use. Also that
`INSUFFICIENT_PERMISSIONS` under 401 is a user's permissions, that an
unknown voice is 400 `Bad Request`, and that a failed WebSocket upgrade
puts its error in the response body and the `dg-error` header. Facts and
two short quoted messages are taken, and no code. The documentation's own
licence was not read (unverified 2026-11-27).

## PX4's ULog and Home Assistant's traces

**PX4.** The ULog file format, <https://docs.px4.io/main/en/dev_log/ulog_file_format.html>
(both sentences below read 2026-11-27). The PX4-Autopilot repository is
**BSD-3-Clause** (GitHub API, verified 2026-11-27); the documentation's own
licence is unread. **Home Assistant.** Automation traces,
<https://www.home-assistant.io/docs/automation/testing/> (verified 2026-11-25
by the survey); the documentation repository is **CC BY-NC-SA 4.0** (its
LICENSE.md, read 2026-11-27), so the idea is taken and none of its words.

**What Emet took.** Two ideas, and no code. From ULog, a file that describes
itself in its header ("The format is self-describing") and a reader that
refuses a version it does not know ("If a parser finds any of these bits set
that isn't specified, it must refuse to parse the log"): the `--events`
header names its format, version, packages and platform, and a reader refuses
a format other than `emet-events` or a major other than 1
([`emet_engine/events.py`](emet-engine/emet_engine/events.py)). From Home
Assistant, a run's record kept as a file a person can download, read and
share. The file is JSON lines from the standard library.

## LiveKit Agents and Pipecat

**LiveKit Agents** (<https://github.com/livekit/agents>, **Apache-2.0**) and
**Pipecat** (<https://github.com/pipecat-ai/pipecat>, **BSD-2-Clause**), each
licence read through the GitHub API (verified 2026-11-27). One idea from
each, no code. LiveKit's `tts_text_transforms`: "filter_markdown strips
Markdown formatting", "filter_emoji removes emoji characters", and "When
tts_text_transforms is not set, both are applied by default"
(<https://docs.livekit.io/agents/build/text/>, verified 2026-11-27).
Pipecat's `MarkdownTextFilter` "Preserves numbered lists while removing
Markdown formatting" (<https://docs.pipecat.ai/server/utilities/text/markdown-text-filter>,
verified 2026-11-27; deprecated since 1.14.0 for `strip_markdown`). Taken:
a pass between the reply's text and the voice that removes markdown and
emoji and keeps numbered lists, `speakable()` in
[`emet-engine/emet_engine/speech.py`](emet-engine/emet_engine/speech.py).
Reading a URL as its host, the patterns and the code are Emet's.

## Speaking a given line with no model

**edgevox**, <https://github.com/nrl-ai/edgevox> at 5fa958e: its
`LICENSE` file is **Apache-2.0** and its `pyproject.toml` (`license =
"MIT"`) and README say **MIT** (the local clone, read 2026-11-27);
**Home Assistant**, <https://github.com/home-assistant/core>, and **Linux
Voice Assistant**, <https://github.com/OHF-Voice/linux-voice-assistant>,
both **Apache-2.0** (GitHub's licence API, verified 2026-11-27). One idea,
no code: a command that says a given text in the robot's voice and asks no
language model. edgevox's `/say TEXT` "Synthesize and play text directly
(skip LLM)" (`docs/documentation/commands.md` line 23); Home Assistant's
`assist_satellite.announce` and Linux Voice Assistant's announcements have
a speaker say a message. Emet's `emet-say`
([`emet_engine/say.py`](emet-engine/emet_engine/say.py)) boots the voice and
the sink only and performs the text's tags through their chains.

## flake8, pyflakes, pycodestyle and mccabe

CI-only tools of the PyCQA, pinned by hash in
[`tools/ci/requirements.txt`](tools/ci/requirements.txt) and never imported
by the four packages. Licences and upload dates from PyPI's JSON API,
verified 2026-11-27.

- **Tarek Ziadé, Ian Stapleton Cordasco and the flake8 contributors.**
  flake8 7.5.0, <https://github.com/PyCQA/flake8>, **MIT**. The `layering`
  job of [`tools/ci.py`](tools/ci.py) runs it with
  `--select=E402,E731,F401,F821` (0.5.7-D14); the default exclude it keeps
  was read in the wheel's `defaults.py`.
- **pyflakes 4.1.0** (<https://github.com/PyCQA/pyflakes>, **MIT**),
  **pycodestyle 2.15.0** (<https://github.com/PyCQA/pycodestyle>, **MIT**)
  and **mccabe 0.7.0** (<https://github.com/PyCQA/mccabe>, **MIT**, which
  its metadata calls the Expat licence). No code is copied.

## The vendors' models endpoints

What [`tools/bank/canary.py`](tools/bank/canary.py)'s lifecycle step reads,
each page read 2026-11-27. Only the endpoint, its header and its field names
are taken.

- **OpenAI**, API reference, "Retrieve model", `GET /v1/models/{model}`,
  field `shutdown_date`,
  <https://developers.openai.com/api/reference/resources/models/methods/retrieve>.
- **Anthropic**, Claude API reference, "Get a Model",
  `GET /v1/models/{model_id}`, fields `lifecycle` and `retires_at`,
  <https://platform.claude.com/docs/en/api/models>.
- **Deepgram**, API reference, "List Models", `GET /v1/models`,
  `canonical_name`, <https://developers.deepgram.com/reference/manage/models/list>;
  its "Get a Model" takes a UUID
  (<https://developers.deepgram.com/reference/manage/models/get>).

## RFC 5737

**J. Arkko, M. Cotton and L. Vegoda.** RFC 5737, IPv4 Address Blocks
Reserved for Documentation, January 2010,
<https://www.rfc-editor.org/rfc/rfc5737.html>. Copyright (c) 2010 IETF
Trust and the persons identified as the document authors. 192.0.2.0/24,
TEST-NET-1, is the address
[`tools/tests/test_hardware_guard.py`](tools/tests/test_hardware_guard.py)
has the guard refuse. Only the reserved block is taken.

## ECMA-48

**Ecma International.** *ECMA-48, Control Functions for Coded Character
Sets*, 5th edition, June 1991,
<https://ecma-international.org/publications-and-standards/standards/ecma-48/>,
free to download; its text holds no licence statement (the PDF read
2026-11-27). One control function, no text: EL, ERASE IN LINE (section
8.3.41, `CSI Ps 04/11`, default `Ps = 0`, the active position to the end
of the line). The live line
([`emet_engine/live.py`](emet-engine/emet_engine/live.py)) is drawn as a
carriage return, its text and `ESC [ K`, and cleared by a carriage return
and the same erase.

## Farina's swept sine

**A. Farina.** "Simultaneous measurement of impulse response and distortion
with a swept-sine technique", 108th AES Convention, Paris, February 2000,
paper 5093 (AES e-library 10211, its record read 2026-11-27). One idea and
its formula, no code: an exponential sine sweep for a room response, with
the phase `sin(2*pi*f1*T/L*(exp(t*L/T)-1))` and `L = ln(f2/f1)`, in
[`tools/bank/sweep.py`](tools/bank/sweep.py). The paper is paywalled and was
not read, so the formula is the method's usual form, and no licence was
read; a test holds the sweep's frequency to `f1*(f2/f1)**(t/T)` within 6%.

---

## Adding to this file

If a change takes an idea, a finding, a number, or a data format from outside
work, add it here and cite it at the point of use. See
[CONTRIBUTING.md](CONTRIBUTING.md#citing-outside-work). Two rules that are easy
to get wrong:

- **Ideas count, as code does.** Using a paper's measurement to pick a
  constant is taking something, even though nothing was copied.
- **Record the licence of what was taken**, not just of the repository it came
  from. A project can ship an MIT tool alongside a corpus you may not use
  commercially, and only one of those is safe to build on.
