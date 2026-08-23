# Contributing to Emet

Emet is Apache 2.0 throughout — SDK, HAL, and engine. Fork it, ship it, sell
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

That line is the [Developer Certificate of Origin](https://developercertificate.org/)
— a statement that you wrote the change, or have the right to submit it under
Apache 2.0.

**We use a DCO rather than a CLA on purpose.** A CLA asks someone to sign a
legal document before fixing a typo, and most of the people who will improve
this project are building robots in spare rooms. The trade we accept in return
is that nobody, including the maintainer, can unilaterally relicense Emet — any
future licence change needs the agreement of everyone who has contributed. For
a project whose pitch is that it will not be taken away, that is the right way
round.

To sign off work you have already committed:

```sh
git rebase --signoff HEAD~3     # last three commits
```

## What is open to contribution, and what is not

Not everything in this repository is equally safe to change, because not
everything is equally cheap to get wrong.

**Welcome, and the reason the project is open:**

- **Drivers** in `emet-hal` — servos, displays, LEDs, sensors, motor drivers.
  None are written yet; `emet_hal.mock` shows the shape one takes.
- **Locomotion plugins** — new kinematics. `drive.kinematics` is an open enum
  precisely so that `legged`, `omni`, and things nobody has thought of can
  arrive as packages rather than as schema changes.
- **Souls** — `soul.yaml` bundles and starter memory.
- **Motion packs** — recorded clips for particular bodies.
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

Not a hierarchy of importance — a hierarchy of *reversibility*. A driver that
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
   distinct — conflating a typo with absent hardware costs support hours.

See [DESIGN.md](DESIGN.md) §2 for the full set and the reasoning behind each.
If a change seems to require breaking one of these, open an issue rather than
a pull request — that conversation is usually more interesting than the patch.

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
hears, so a vulnerability here is a vulnerability in someone's home. Contact
the maintainer directly.
