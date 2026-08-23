# Emet

**A companion-robot engine that discovers whatever body it finds itself in.**

Write a personality once. Run it on a robot with a head, wheels, and glowing
eyes — or on a Raspberry Pi with nothing but a speaker. The same personality,
the same memories, and a robot that *knows the difference* and will tell you
about it.

*Emet* (Hebrew אמת, "truth") is the word inscribed on the golem's forehead to
bring it to life; erase the first letter and it reads *met*, "death". The name
is a promise: the thing you are talking to is honest about what it is.

---

## Status: 0.2 — early

Emet does not yet listen, speak, remember, or move. Nothing here drives a servo.

What it does today is answer one question — **given a robot, what would each
intent mean on it?** — which turns out to be the whole product in miniature.

```sh
$ emet explain examples/bodiless.yaml
BINDING TABLE  —  examples/bodiless.yaml   (body: bodiless)
0 capabilities, 31 intents, 0 bound to hardware, 31 to voice

    express.curiosity  voice   inflect   preset=rising filler=['hm?', 'hmm.']

$ emet explain examples/mock-scout.yaml
BINDING TABLE  —  examples/mock-scout.yaml   (body: mock_scout)
5 capabilities, 31 intents, 30 bound to hardware, 1 to voice

    express.curiosity  head    tilt      angle_deg=12 speed=0.4 hold_ms=700
```

Same personality, same configuration, two bodies. Nobody wrote an `if`
statement.

## How it works

A personality emits **intents** — about thirty things it might want, like
`express.curiosity` or `attend.speaker`. It never names hardware; that is what
makes it portable.

Each intent has a **chain**: a ladder of ways to perform it, best first.

```yaml
express.curiosity:
  rungs:
    - actuator: {role: head, axis: pitch}     # tilt your head
      action: tilt
    - actuator: {role: eyes}                  # or squint
      action: expression
    - actuator: {role: ambient, type: light}  # or pulse a light
      action: pulse
    - actuator: {voice: true}                 # or just say "hm?"
      action: inflect
```

At boot, Emet walks each ladder against the robot it is actually running on and
binds the first rung that fits.

**Every chain must end in a voice rung, and the validator refuses to load one
that does not.** Since every Emet robot is required to have a speaker, the last
rung can never fail — which turns "no intent can fail for lack of hardware"
from a promise someone has to remember into something the software enforces.

## Try it

```sh
python -m venv .venv
.venv/bin/pip install -e "emet-sdk[dev]" -e "emet-hal[dev]"   # Windows: .venv\Scripts\pip

cd emet-sdk
emet validate examples/mock-scout.yaml --verify-drivers
emet explain  examples/scout-01.yaml --why
```

`--why` is the one to remember. When a robot is not doing what you expected, it
tells you which rungs were skipped and what was wrong with each:

```
  ~ express.affection  eyes  expression  hold_ms=1500 preset=soft
      skipped rung 0 {role: head, axis: roll}: 'head' has no 'roll' axis; it has pitch, yaw
```

## Layout

| | |
|---|---|
| `emet-sdk/` | Types, schemas, the intent vocabulary, chain resolution. The contract everything agrees on. |
| `emet-hal/` | Drivers and locomotion plugins. Where hardware support goes. |
| `emet-engine/` | Personality, memory, arbitration. Does not exist yet. |

## Documentation

- **[DESIGN.md](DESIGN.md)** — the specification. Manifest format, intent
  vocabulary, memory model, plugin contract, and why each decision went the way
  it did.
- **[CONTRIBUTING.md](CONTRIBUTING.md)** — what is open to contribution, the
  design rules that are not negotiable, and how to run what CI runs.
- **[TRADEMARK.md](TRADEMARK.md)** — the code is yours to fork; the name is not.

## Licence

Apache 2.0, throughout — SDK, HAL, and engine alike. Fork it, ship it, sell it.

The name is the exception, and deliberately so: a permissive licence means
anyone may fork Emet and close their fork, so the mark is the only thing that
keeps "I run Emet" meaning something specific. See
[TRADEMARK.md](TRADEMARK.md).
