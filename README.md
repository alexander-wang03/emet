# Emet

**A companion-robot engine that discovers whatever body it finds itself in.**

Write a personality once. Run it on a robot with a head, wheels, and glowing
eyes, or on a Raspberry Pi with nothing but a speaker. The same personality,
the same memories, and a robot that *knows the difference* and will tell you
about it.

*Emet* (Hebrew אמת, "truth") is the word inscribed on the golem's forehead to
bring it to life; erase the first letter and it reads *met*, "death". The name
is a promise: the thing you are talking to is honest about what it is.

---

## Status: 0.4 (early)

Emet talks. Say its name, ask it something, and a voice answers. It does not
yet remember or move, and nothing here drives a servo.

What it does today is two things. It holds a conversation: it hears its own
name, works out when you have finished speaking, sends the words to a speech
recognition provider, hands the transcript to a language model with its
persona as the prompt, and speaks the reply a sentence at a time while the
rest is still being written. The providers are plugins the soul chooses, the
keys are your own, and there is no default provider.

```sh
$ emet-talk examples/pi-speakerphone.yaml examples/emet-soul.yaml
Emet is listening for 'hey emet'
  wake     pocketsphinx on microphone, 16000 Hz
  words    deepgram, nova-3
  answers  openai, gpt-5.6-terra
  voice    piper, en_US-ljspeech-medium (22050 Hz)
  patience 900 ms, extends once on a trailing clause
  (ctrl-c to stop)

  (heard 'hey emet')
  you:  Do you know where the capital of Mongolia is?
  emet: The capital of Mongolia is Ulaanbaatar.
```

It does not remember what you said last time, and it cannot act on anything
it says: the intents its reply carries are lifted out and reported, and
nothing moves until 0.5 gives it a self-model and a body something to do
with them.

And it answers the question the whole design rests on: **given a robot, what
would each intent mean on it?**

```sh
$ emet explain examples/bodiless.yaml
BINDING TABLE  examples/bodiless.yaml   (body: bodiless)
0 capabilities, 31 intents, 0 bound to hardware, 31 to voice

  ~ express.curiosity  voice        inflect      filler=['hm?', 'hmm.'] preset=rising

$ emet explain examples/mock-scout.yaml
BINDING TABLE  examples/mock-scout.yaml   (body: mock_scout)
5 capabilities, 31 intents, 30 bound to hardware, 1 to voice

    express.curiosity  head         tilt         angle_deg=12 hold_ms=700 speed=0.4
```

Same personality, same configuration, two bodies. Nobody wrote an `if`
statement.

## How it works

A personality emits **intents**: about thirty things it might want, like
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
rung can never fail, which turns "no intent can fail for lack of hardware"
from a promise someone has to remember into something the software enforces.

## Try it

```sh
python -m venv .venv
.venv/bin/pip install -e "emet-sdk[dev]" -e "emet-hal[dev]" -e "emet-providers[dev]" -e "emet-engine[dev]"   # Windows: .venv\Scripts\pip

cd emet-sdk
emet validate examples/mock-scout.yaml --verify-drivers
emet explain  examples/scout-01.yaml --why
```

To hear it wake, add the optional microphone and wake-engine extras and run the
listen loop. On Linux the `audio` extra needs the system PortAudio
(`apt install libportaudio2`).

```sh
pip install -e "emet-hal[audio,wake]"
emet-listen examples/scout-01.yaml examples/emet-soul.yaml
```

To hear it answer, install the providers the reference soul names, put your
keys in `~/.config/emet/keys.env`, download the voice once, and run the whole
loop with no flags. `emet-providers/README.md` has the details.

```sh
pip install -e "emet-providers[deepgram,openai,piper]"
python -m piper.download_voices en_US-ljspeech-medium --data-dir ~/.local/share/emet/voices
emet-talk examples/pi-speakerphone.yaml examples/emet-soul.yaml
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
| `emet-providers/` | The plugins that reach a service, or a model on disk: speech recognition, language models and voices. |
| `emet-engine/` | The loop: wake, endpointing, words, answer, voice, audio in and out. The self-model, memory and arbitration arrive from 0.5. |

## Documentation

- **[DESIGN.md](DESIGN.md)**: the specification. Manifest format, intent
  vocabulary, memory model, plugin contract, and why each decision went the way
  it did.
- **[CONTRIBUTING.md](CONTRIBUTING.md)**: what is open to contribution, the
  design rules that are not negotiable, and how to run what CI runs.
- **[RELEASING.md](RELEASING.md)**: the checklist every release goes through,
  and what went wrong to put each item on it.
- **[CITATIONS.md](CITATIONS.md)**: outside work whose ideas, findings, or data
  shaped Emet, and the licence attached to each.
- **[TRADEMARK.md](TRADEMARK.md)**: the code is yours to fork; the name is not.

## Licence

Apache 2.0 throughout: SDK, HAL, providers and engine alike. Fork it, ship
it, sell it.

The name is the deliberate exception. A permissive licence means anyone may
fork Emet and close their fork, so the mark is the only thing keeping "I run
Emet" meaningful. See
[TRADEMARK.md](TRADEMARK.md).
