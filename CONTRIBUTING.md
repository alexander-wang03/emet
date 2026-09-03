# Contributing to Emet

Emet is Apache 2.0 throughout: SDK, HAL, and engine. Fork it, ship it, sell
it. The only thing not covered by that licence is the *name*: see
[TRADEMARK.md](TRADEMARK.md).

## Sign your commits (DCO)

Every commit needs a `Signed-off-by` line:

```sh
git commit -s -m "Add mg996r driver"
```

which appends:

```
Signed-off-by: Your Name <your.email@example.com>
```

That line is the [Developer Certificate of Origin](https://developercertificate.org/):
a statement that you wrote the change, or otherwise have the right to submit it
under Apache 2.0.

### Never thinking about this again

Most contributors should never have to remember the flag:

- **Editing on github.com** — nothing to do. Web commits are signed off for you.
- **VS Code** — nothing to do. This repository ships a `.vscode/settings.json`
  that turns on `git.alwaysSignOff`, and the built-in Git UI honours it.
- **Command line** — use `git commit -s`. If you forget, fix it afterwards
  rather than redoing the work:

  ```sh
  git rebase --signoff origin/master
  ```

**There is no git config for this, and looking for one wastes an afternoon.**
`commit.signoff` does not exist; git accepts it into your config file and
silently ignores it. `format.signoff` is real but applies only to
`git format-patch`, never to `git commit`. Sign-off comes from the `-s` flag, a
`prepare-commit-msg` hook, or a client passing the flag on your behalf.

Signing off is also unrelated to *signing*. `commit.gpgsign` produces a
cryptographic signature; `Signed-off-by` is a line of text asserting the DCO.
A commit can have either, both, or neither.

### Why a DCO and not a CLA

A CLA asks someone to sign a legal document before fixing a typo, and most of
the people who will improve this project are building robots in spare rooms.

Two things are easy to conflate here, and the distinction decides what this
section actually buys:

- **Nobody, including the maintainer, can unilaterally relicense Emet.** That
  follows from there being *no CLA*: contributors keep their copyright and
  grant Emet nothing beyond Apache 2.0, so any future licence change needs the
  agreement of everyone who has contributed. The DCO does not produce this
  property and it would hold without it.
- **The DCO's own job is narrower**: a per-commit record that you had the right
  to submit what you submitted. Apache 2.0 §5 already places contributions
  under this licence by default. What it does not capture is the assertion that
  the code was yours to give, and that is the gap the sign-off fills.

For a project whose pitch is that it cannot be taken away, the first property
is the load-bearing one, and it is worth knowing which mechanism provides it.

## What is open to contribution, and what is not

Not everything in this repository is equally safe to change, because not
everything is equally cheap to get wrong.

**Welcome, and the reason the project is open:**

- **Drivers** in `emet-hal`: servos, displays, LEDs, sensors, motor drivers.
  None are written yet; `emet_hal.mock` shows the shape one takes.
- **Locomotion plugins**: new kinematics. `drive.kinematics` is an open enum
  precisely so that `legged`, `omni`, and things nobody has thought of can
  arrive as packages rather than as schema changes.
- **Souls**: `soul.yaml` bundles and starter memory.
- **Motion packs**: recorded clips for particular bodies.
- **Documentation**, especially anything that assumes embedded experience a
  newcomer will not have.

**Discuss in an issue before opening a PR:**

- **The intent vocabulary** (`emet_sdk/intents.py`). It is a *closed* set, so
  adding a name is a minor version bump and an unrecognised name makes a soul
  bundle invalid rather than merely ineffective.
- **The manifest and bundle schemas.** A field added today costs nothing; a
  field added once strangers' robots depend on it costs a migration across
  every SD card in the field. This asymmetry is the reason the whole schema was
  written before the engine existed.
- **Capability types.**

The ordering is by *reversibility*, not importance. A driver that
turns out to be wrong is reverted in a minute. A schema field that turns out to
be wrong is with us for years.

## Ground rules that are not negotiable

These are design invariants, not preferences. A change that breaks one is
wrong even if it works:

1. **The soul never names hardware.** No servo channel, GPIO pin, I2C address,
   or driver name may appear in the soul layer. It emits intents.
2. **Every fallback chain terminates in a voice rung.** The validator enforces
   this. It is what makes "every intent is always satisfiable" mechanical
   rather than aspirational.
3. **`emet_sdk` imports nothing internal.** `emet_hal` and `emet_engine` import
   `emet_sdk` only. CI checks this on every push.
4. **Memory is never namespaced by body.** Experiences travel with the soul;
   hardware conditions stay with the body.
5. **A missing plugin is not a schema error.** Keep the two failure modes
   distinct. Conflating a typo with absent hardware costs support hours.

See [DESIGN.md](DESIGN.md) §2 for the full set and the reasoning behind each.
If a change seems to require breaking one of these, open an issue rather than
a pull request: that conversation is usually more interesting than the patch.

## Running things

```sh
python -m venv .venv
.venv/bin/pip install -e "emet-sdk[dev]" -e "emet-hal[dev]"
```

Install **both** packages even if you are only touching one. Plugin discovery
reads entry points, so several SDK tests are meaningless unless something is
registered to be discovered.

Before opening a PR, run what CI runs:

```sh
python tools/check_layering.py .
cd emet-sdk && python -m pytest -q
cd ../emet-hal && python -m pytest -q
```

To see what your driver actually binds to, without a robot:

```sh
cd emet-sdk && emet explain examples/mock-scout.yaml --why
```

## Reporting a security issue

Do not open a public issue. Emet listens continuously and remembers what it
hears, so a vulnerability here is a vulnerability in someone's home.

Use [GitHub's private vulnerability reporting](https://github.com/alexander-wang03/emet/security/advisories/new),
or email <wake.up.emet@gmail.com>. See [SECURITY.md](SECURITY.md) for what
counts and what does not.
