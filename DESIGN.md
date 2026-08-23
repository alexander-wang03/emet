# Emet — Design Specification

**Emet** (Hebrew אמת, "truth") is a companion-robot engine. It installs onto a
Raspberry Pi, discovers whatever body it finds itself in, and runs a portable
personality — a *soul* — on top of it.

In the Golem tradition the word *emet* inscribed on the clay is what brings it
to life; erasing the first letter leaves *met* ("death") and it returns to
stillness. The name is the product: an animating word that turns inert matter
into something alive, and a promise that the thing you are talking to is honest
about what it is.

This document specifies how Emet works — the manifest format, the intent
vocabulary, the fallback mechanism, the memory model, and the plugin contract.
It is the reference for anyone writing a driver, authoring a soul, or trying to
understand why a design decision went the way it did.

**What this document is not.** It does not contain release dates, delivery
plans, or commercial strategy. Section numbering is stable and deliberately
sparse at the start; internal cross-references depend on it.

---

## 0. How to read this document

Every schema field and subsystem carries a tag describing **how finished it
is** — not when it will arrive:

| Tag | Meaning |
|---|---|
| **`P0`** | Implemented. The engine reads this field and acts on it. |
| **`RSV`** | Reserved. The field exists in the schema and validators accept it, but the engine ignores it. |
| **`V1`** | End-goal. Not in the schema yet — listed so that today's design does not foreclose it. |

**`RSV` is the important one, and the reason this document exists at all.**
Schema changes are expensive once other people's robots depend on them, while
unused schema fields are free. So the whole shape is written down first and
implemented gradually. A field reserved today costs nothing; the same field
added after a hundred strangers have robots running Emet costs a migration on
every one of their SD cards.

Consequence worth stating plainly: **finding an `RSV` field is not finding a
bug.** It means the shape was settled early on purpose.

---

## 1. The product and the soul line

### 1.1 The product

**Emet.** The engine. The open-source runtime that does personality synthesis,
memory, arbitration, and safety. When someone says "I run Emet on my robot,"
this is what they mean.

### 1.2 The soul line (default personality presets)

Emet ships blank-slate, but the project publishes a small family of **reference souls** — complete `soul.yaml` + starter memory, same engine, different default character. These are the recognizable faces of the platform, the thing a newcomer installs before writing their own. Each is named for a word meaning "spirit" or "animating principle," reinforcing that they are *variations on a soul*.

| Soul | Root | Default character sketch |
|---|---|---|
| **Emet** | Hebrew, "truth" | The earnest one. Plain-spoken, curious, honest to a fault. The house character; also the reference implementation for docs. |
| **Hugr** | Old Norse, "mind, will" | The willful one. Opinionated, a bit stubborn, initiates more. Higher `curiosity` and lower `formality`. |
| **Neuma** | from Greek *pneuma*, "breath, spirit" | The airy one. Gentle, reflective, sparing with words. Higher `warmth`, low `verbosity`, long `patience_ms`. |

`RSV`: the soul line is data, not code — new reference souls are just published bundles. Nothing in the engine hardcodes them.


---

## 2. Design principles

The invariants. If an implementation decision violates one of these, the decision is wrong.

1. **The soul never names hardware.** No component of the soul layer may reference a servo channel, a GPIO pin, an I2C address, or a driver. It emits intents. Violating this makes souls non-portable, which is the entire product.
2. **Every intent is always satisfiable.** Because the hardware floor is a microphone and a speaker, every expressive fallback chain terminates in a voice rung. No intent may fail for lack of hardware; it degrades.
3. **The body is part of the character.** The manifest is compiled into the soul's self-description and injected into its context. A robot without wheels knows it cannot come to you, and says so, unprompted.
4. **Reflexes are local, forever.** Anything on a sub-200ms loop — wake word, VAD, speaker ID, gaze tracking, servo control, safety limits — never leaves the Pi, regardless of connectivity or cost.
5. **Degradation is declared, not improvised.** Fallbacks are data in the manifest and intent library, resolved once at boot, not `if hasattr(...)` scattered through the engine.
6. **Truth is the brand.** Per the name: the memory file is readable, the self-model is honest about the body's limits, and the robot does not claim capabilities it lacks. This is a design constraint, not just marketing.

---

## 3. Package layout

**Emet is open source, in full.** One monorepo, three packages, one permissive license.
The boundary between the packages is a product surface; changes to it are breaking changes.
See `CONTRIBUTING.md` for what is open to contribution and what is not.

```
emet/                         one repository, Apache 2.0 throughout
  emet-sdk/         the contract layer
    schemas/          manifest, soul bundle, motion pack (JSON Schema)
    emet_sdk/
      types.py        Intent, Action, CapabilityDescriptor, Pose, Twist
      plugin.py       ActuatorPlugin, SensorPlugin, LocomotionPlugin ABCs
      intents.py      the canonical intent vocabulary
      chains.py       fallback chain format + the voice-rung rule
      discovery.py    entry-point plugin discovery
      resolve.py      chain resolution → the binding table (§6)
      validate.py     manifest + bundle validators

  emet-hal/         community-contributed
    mock.py           an actuator and a sensor that pretend
    differential.py   two independently driven wheels  (§12.1)
    tracked.py        the same arithmetic, plus tread scrub
    ...               pca9685, tb6612, gc9a01, ws2812 — not yet written

  emet-engine/      personality synthesis, memory, arbitration,
                    choreography, prompting, safety, consolidation.
```

**HAL** is *hardware abstraction layer* — the standard embedded and OS term for the layer separating generic upper software from specific silicon. Emet uses it in Android's sense: the abstraction itself is `emet_sdk.plugin` (the ABCs and capability descriptors), and `emet-hal` is the collection of per-device *implementations* that satisfy it. Spell the acronym out on first use in any document a newcomer might read first; not every contributor arrives from embedded work.

The engine imports the SDK. Plugins import the SDK. The SDK is types and contracts and almost no logic — target under ~2,000 lines. It is the only thing both sides must agree on. Enforced in CI by `tools/check_layering.py`, because with one open monorepo the layering is a test rather than a property of how the software is distributed.

**Where the line falls when something is arguably logic:** deterministic pure functions over contract data belong in the SDK; anything stateful, scheduled, or personality-bearing belongs in the engine. Chain resolution (§6) is the former — `(chains, descriptors) → binding table`, no state, no I/O — and lives in the SDK so that a HAL contributor can check where their driver binds without running an engine. The choreographer, at 50Hz and holding motion state, is the latter.

**One license, permissive, everywhere — settled August 2026.** Apache 2.0 for the SDK, the HAL, and the engine alike. There is no licence boundary inside the repository and nothing about it to explain to a contributor. A hobbyist may do anything with it, and so may a company.

The consequence to hold onto: **anyone may fork Emet, close their fork, and ship it.** That is permitted, not accidental. The only thing that stops such a fork calling itself *Emet* is the trademark. See `TRADEMARK.md`.

**`P0`** — everything public from the first commit, under its final license. Open is a one-way door: once published, it is published, and a permissive release can never be walked back for code already out.

**Layering is enforced by CI, not by a license wall.** An import linter asserts that `emet-sdk` imports nothing internal, `emet-hal` imports only `emet-sdk`, and `emet-engine` imports only `emet-sdk`. Previously this invariant was maintained by the engine being a separate closed artifact; that structural guarantee is now a test, and it must actually run in CI or it will rot.

Python import namespace: `emet_sdk`, `emet_hal`. CLI binary: `emet`. Config root: `/etc/emet/`. Soul bundles: `*.emet` directories.

---

## 4. Body Manifest

**File:** `/etc/emet/body.yaml`
**Written by:** hardware autodiscovery, then conversational calibration, then hand-editing.
**Read by:** the engine at boot, and never again until restart.

### 4.1 Top-level structure

```yaml
manifest_version: "0.1"

body:
  id: "scout-01"                 # P0  stable; keys body-local state, below.
                                 #     NEVER a memory namespace.
  name: "wheeled desk scout"     # P0  human label, not the robot's name
  scale: desk                    # P0  desk | floor | large
  power: plugged_in              # P0  plugged_in | battery
  battery_capacity_wh: null      # RSV
  description: >                 # P0  free text, feeds the self-model
    A small treaded robot about the size of a coffee mug, with a
    head that turns and two round display eyes.

compute:
  platform: raspberrypi5         # P0
  ram_gb: 8                      # P0
  accelerator: none              # RSV  none | hailo8l | coral

audio:                           # P0  required — this is the hardware floor
  input:
    device: "plughw:1,0"
    sample_rate: 16000
    channels: 4
    aec: hardware                # P0  hardware | software | none
    doa: true                    # P0  direction of arrival available
  output:
    device: "plughw:1,0"
    gain_db: -6.0

capabilities: []                 # P0  see 4.2

safety:                          # P0
  estop_gpio: null
  max_continuous_motion_s: 30
  thermal_throttle_c: 75
```

**If `capabilities` is empty, the robot is still valid.** A Pi with a USB speakerphone and nothing else runs the identical soul. Every intent resolves to its voice rung. This case must work on day one — it is the proof that the abstraction is real, and it is the first thing the project builds.

#### What `body.id` is and is not

**`body.id` never appears in `memory.db`.** Memories, people, and episodes belong to the *soul* and travel with the bundle. If retrieval were scoped by body, moving a bundle to a new chassis would silently return zero rows and the robot would greet its owner of six months as a stranger — destroying the flagship demo (§8) in the most disheartening way available, with no error to diagnose.

`body.id` keys **body-local state**: the things that are meaningless or actively wrong on a different body.

| Body-local state | Why it cannot travel |
|---|---|
| `trim_deg` per joint | Calibrated to one servo horn spline |
| Audio device paths | Different USB enumeration |
| DoA reference angle, mic gain | Different array, different mounting |
| Camera intrinsics | Different lens |
| Odometry corrections | The next body may have no wheels |
| Driver health and fault history | About specific hardware |
| Resolved binding table cache | Derived from this manifest |

**Location:** `/etc/emet/state/<body.id>.json`, beside `body.yaml`. Never inside the bundle. This is what makes one Pi swappable between two chassis without their calibrations colliding — the actual and only job of `body.id`.

#### Experiences travel; conditions do not

The distinction that keeps this clean once proprioception arrives (§7 `V1`):

- *"I couldn't come when you called, and you laughed at me"* is a shared episode. It is a memory, with `subject_person_id = '@self'`, and it travels to the lamp body where it will never happen again.
- *"Left wheel drawing high current"* is a hardware condition. It is body-local state, and it does not follow the soul anywhere.

Fixing this line now is what lets `V1` proprioceptive self-model updates ship later without a memory migration.

### 4.2 Capability blocks

Each entry in `capabilities` is a typed block. `P0` types:

#### `joint_group`

```yaml
- id: head
  type: joint_group
  role: head                     # P0  head | arm | torso | custom
  driver:
    plugin: emet_hal.pca9685
    params: {bus: 1, address: 0x40, pwm_freq: 50}
  joints:
    - id: pan
      axis: yaw                  # P0  yaw | pitch | roll | linear
      channel: 0
      range_deg: [-90, 90]
      home_deg: 0
      max_speed_dps: 180
      invert: false
      trim_deg: 0                # set by calibration
    - id: tilt
      axis: pitch
      channel: 1
      range_deg: [-30, 45]
      home_deg: 0
      max_speed_dps: 120
  parent: null                   # RSV  kinematic chain parent
```

Declared axes are what let the choreographer generate motion without user code. A `yaw` joint on a `head` group is enough for the engine to know how to look toward a bearing.

#### `drive`

```yaml
- id: base
  type: drive
  kinematics: differential       # P0  OPEN ENUM — any string naming an
                                 #     installed locomotion plugin.
                                 #     Built-in P0: differential | tracked
                                 #     Later: omni | ackermann | legged | ...
  driver:
    plugin: emet_hal.tb6612
    params: {pwm_left: 12, pwm_right: 13, dir_left: [5,6], dir_right: [16,20]}
  geometry:
    wheel_radius_m: 0.032
    track_width_m: 0.14
  limits:
    max_linear_mps: 0.35
    max_angular_rps: 2.0
    accel_limit_mps2: 0.5
  odometry: none                 # P0  none | encoders
                                 # RSV imu_fused | slam
```

`differential` and `tracked` share math and differ only in slip assumptions on turns.
They ship as the two built-in locomotion plugins.

**`kinematics` is an open enum.** It is a string naming a locomotion plugin, not a value
from a frozen list. The engine does not contain wheels-and-treads logic; it asks a
locomotion plugin "how do I move?" and the plugin answers. `legged` is therefore already
expressible — it is simply a plugin nobody has written yet. See §7.1 for the reasoning and
§12.1 for the plugin category.

#### `drive` — legged bodies (`RSV`)

Reserved now, ignored by the P0 engine, so that no manifest anywhere needs to change when
a legged plugin arrives:

```yaml
- id: base
  type: drive
  kinematics: legged             # accepted today; plugin comes later
  legs:                          # RSV — reserved, ignored by the P0 engine
    count: 2
    dof_per_leg: 3
    gait: static                 # RSV  static | dynamic
    balance: none                # RSV  none | imu_feedback
```

#### `display`

```yaml
- id: eyes
  type: display
  role: eyes                     # P0  eyes | face | status | screen
  form: dual_round               # P0  dual_round | single_round | rect | matrix
  driver:
    plugin: emet_hal.gc9a01
    params: {bus: spi0, cs: [8, 7], dc: 25, rst: 24}
  resolution: [240, 240]
  refresh_hz: 30
```

#### `light`

```yaml
- id: ring
  type: light
  role: ambient                  # P0  ambient | status | eyes
  form: ring                     # P0  ring | strip | single
  count: 16
  driver:
    plugin: emet_hal.ws2812
    params: {gpio: 18}
```

#### `camera`

```yaml
- id: front_cam
  type: camera
  role: front                    # P0
  driver:
    plugin: emet_hal.libcamera
    params: {index: 0}
  resolution: [1280, 720]
  fov_deg: 66
  mounted_on: head               # P0  capability id, or "body"
  local_pipeline: [face_detect, motion]   # P0
                                          # RSV pose, hand, object
```

`mounted_on` matters: a camera on a pan/tilt head lets the engine implement "look at what you're talking about" by driving the head, not just cropping the frame. Frames are grabbed on demand and sent to a cloud VLM only on an explicit conversational trigger; video is never streamed. (Principle 6: honest about when it is looking.)

#### `sensor`

```yaml
- id: imu
  type: sensor
  sensor_kind: imu               # P0  imu | range | touch | light | temp
  driver: {plugin: emet_hal.mpu6050, params: {bus: 1, address: 0x68}}
  role: orientation
```

**`V1` capability types:** `manipulator` (gripper with grasp state), `audio_extra` (secondary mic arrays for room-scale), `projector`, `thermal`.

### 4.3 Validation rules

Enforced by `emet_sdk.validate`, run at boot and by the CLI:

- `audio.input` and `audio.output` are **required**. There is no valid manifest without them.
- Capability `id` values are unique and match `[a-z][a-z0-9_]*`.
- Every joint's `home_deg` lies within `range_deg`.
- At most one `drive` block (`P0`). **`V1`:** multiple drives for hybrid locomotion.
- `memory.db` contains **no body-identifying column**. Enforced by schema inspection, not convention — see §4.1, "What `body.id` is and is not". A bundle whose memory database carries a body reference is invalid.
- `drive.kinematics` must be a **string that resolves to an installed locomotion plugin** — not a member of a frozen list. An unrecognized value fails boot with "no locomotion plugin provides `legged`; install one or change `kinematics`", which is a *missing plugin* error, not a *schema* error. This is what keeps the enum open.
- Every referenced `driver.plugin` resolves to an installed plugin, or boot fails loudly with the missing package name. Never silently degrade because of a typo — that is a *different* failure from missing hardware, and conflating them costs support hours.

---

## 5. Intent vocabulary

Defined in `emet_sdk.intents`. A **closed set** — the soul may only emit these. Adding one is a minor version bump of the SDK.

```python
@dataclass(frozen=True)
class Intent:
    kind: str                  # "express" | "attend" | "speak" | ...
    argument: str | None       # "curiosity", "speaker", ...
    intensity: float = 0.5     # 0.0–1.0
    target: Target | None = None
    duration_ms: int | None = None
    priority: Priority = Priority.EXPRESSIVE
    interruptible: bool = True
```

### 5.1 `P0` intents

| Kind | Arguments | Notes |
|---|---|---|
| `speak` | text | Carries prosody hints. Always available. |
| `acknowledge` | — | Backchannel. Must fire within 300ms of user endpoint. |
| `attend` | `speaker`, `bearing`, `person:<id>`, `none` | Consumes DoA or vision. Head yaw, or body rotation, or nothing. |
| `express` | `curiosity`, `delight`, `confusion`, `concern`, `amusement`, `boredom`, `surprise`, `affection`, `thinking` | The core expressive set. Deliberately small. |
| `signal` | `booting`, `listening`, `thinking`, `speaking`, `muted`, `offline`, `error` | System state, not emotion. Distinct so a soul can't suppress it. |
| `idle` | `settle`, `look_around`, `fidget`, `doze` | Emitted by the idle loop, lowest priority. |
| `move` | `approach`, `retreat`, `turn_to`, `wander`, `stop` | Only bound if a `drive` exists. |

### 5.2 Reserved intents — `RSV`

`manipulate` (grasp/release/offer), `navigate` (goto a named place, requires mapping), `gesture` (arbitrary named clip from a motion pack), `attend_joint` (look at your own hand — needed for anything resembling embodied reasoning).

**These names are reserved in the vocabulary from `P0`, not added later.** They are legal for a soul to emit; the engine logs them at debug level and drops them. This costs four entries in an enum today.

The reason is the same as every other `RSV` field, but sharper: the intent set is *closed*, so an unrecognized intent is a **validation failure**, not a no-op. If `manipulate` were added to the vocabulary only when it was implemented, a soul bundle authored years earlier that reached for it would fail to load rather than degrade — and bundles are exactly the artifact strangers publish, copy, and keep for years. Reserving the names makes forward-written souls merely ineffective rather than invalid.

`V1` is the *behavior* behind these names, not the names themselves.

### 5.3 Arbitration

Multiple intents will be live at once. Fixed priority, highest wins, ties broken by recency:

| Priority | Value | Examples |
|---|---|---|
| `SAFETY` | 100 | thermal, estop, joint limit, brownout recovery |
| `SIGNAL` | 90 | `signal` — system state is never suppressed |
| `SPEECH` | 80 | `speak`, `acknowledge` |
| `ATTENTION` | 60 | `attend` |
| `EXPRESSIVE` | 40 | `express` |
| `LOCOMOTION` | 30 | `move` |
| `IDLE` | 10 | `idle` |

**`P0`:** one intent per actuator at a time; a higher-priority intent preempts and the lower one is dropped, not queued.
**`V1`:** blending — express curiosity with the eyes *while* attending with the head, per-actuator rather than global.

---

## 6. Fallback chains

The mechanism that makes one soul run any body. Chains live in the SDK as data, are overridable per-soul, and are **resolved once at boot** against the manifest.

```yaml
# emet_sdk/chains/express.yaml
express.curiosity:
  mode: first                    # P0  first | all (RSV)
  rungs:
    - actuator: {role: head, axis: pitch}
      action: tilt
      params: {angle_deg: 12, speed: 0.4, hold_ms: 700}
    - actuator: {role: eyes}
      action: expression
      params: {preset: squint, hold_ms: 700}
    - actuator: {role: ambient, type: light}
      action: pulse
      params: {hue: 190, period_ms: 1200}
    - actuator: {voice: true}    # terminal rung — ALWAYS binds
      action: inflect
      params: {preset: rising, filler: ["hm?", "hmm."]}
```

**Resolution:** at boot, walk each chain top to bottom, bind the first rung whose actuator selector matches something in the manifest. Log the resolved binding table — your single most useful debugging artifact, printable with `emet explain`.

**Validation:** the SDK **rejects** any chain whose final rung is not a voice rung. This is the mechanical enforcement of principle 2.

### 6.1 Voice actions — what the terminal rung actually does

The terminal-rung rule creates a question the rest of the spec does not answer: if
*every* chain ends in voice, what does `idle.doze` sound like on a bodiless robot?
Taken naively, a robot with no actuators would mutter to itself every twenty seconds.

The answer is that a voice rung must always **bind**; it need not always make a
sound. Three voice actions, all `P0`:

| Action | What it does | Used by |
|---|---|---|
| `utter` / `inflect` / `backchannel` / `tone` | Produces sound — speech, a prosodic gesture, a filler, a system tone. | `speak`, `acknowledge`, `express.*`, most `signal.*` |
| `silence` | Binds and produces nothing. | all `idle.*`, `attend.*`, `move.stop` |
| `explain` | Says *why the body cannot comply*. | `move.approach` and `move.retreat` on a body with no drive, `signal.offline`, `signal.error` |

**`silence` is what makes principle 2 survive contact with the idle loop.** §11.1
requires the idle loop to be a silent no-op on a body with no actuators; without a
silent voice action that would have to be a special case in the engine. With one, it
is a data property of the chain, and the engine needs no idle-specific code at all.

**`explain` is principle 6 at the motor level.** Asked to come closer, a robot with no
wheels reaches the bottom of `move.approach` and says so — *"I can't come to you, I
don't have wheels."* The self-model (§7) already told the soul this in context; the
chain layer tells the same truth at the moment it becomes relevant. The two must never
disagree, which is why both are generated from the same manifest.

Consequence worth stating plainly: **a chain ending in `silence` is still a satisfied
intent.** The intent resolved, bound, and executed. Producing no sound is the correct
output, not a failure, and nothing downstream should treat it as one.


**`RSV` — `mode: all`:** bind every matching rung so a well-equipped body tilts its head *and* squints *and* pulses. Needs per-actuator arbitration (5.3), so it waits.

**`V1` — learned chains:** the engine observes which expressions a body renders legibly (via camera self-view or user feedback) and reweights. Listed only to keep the chain format data-driven rather than code.

---

## 7. Self-model generation

At boot, the engine compiles the manifest into a natural-language self-description and injects it into the soul's system context. **`P0`, and the highest value-per-line feature in the spec.** It is also principle 6 made concrete: the robot tells the truth about its own body.

Deterministic template, not an LLM call:

```
YOUR BODY

You are a small treaded robot, about the size of a coffee mug, sitting on a desk.
You can turn your head left and right, and tilt it up and down.
You have two round eyes that can change expression.
You can drive forward, back, and turn in place — slowly, about walking pace
  for an ant.
You cannot see: you have no camera.
You have no arms and cannot pick anything up.
You are plugged into the wall, so you cannot go far.
You hear through a four-microphone array and can tell roughly which direction
  a voice came from.
```

Rules: state capabilities *and* their absence. Absences do more work than presences — they stop the robot from offering to do things it cannot do. Plain physical language, never component names. `scale` and `body.description` feed the phrasing.

**`RSV`:** a `self_model_overrides` block in the soul bundle so a character describes its own body in its own voice.
**`V1`:** proprioceptive updates mid-session — "my left wheel isn't responding" enters context when a driver faults, and the character can *mention* it.

### 7.1 Biped classification — decided

Wheels and treads resolve to motion from declared axes. A biped does not: it is a
balance-control problem — gait generator, state estimator, IMU feedback — that no manifest
resolves. The open question was whether that forces `kinematics` to be a closed enum.

**Decision: `kinematics` is an open enum, and locomotion is a plugin category.** Build the
doorframe now; walk through it later.

Three consequences, all cheap today and all already reflected above:

1. **`kinematics` accepts unknown values.** The validator asks "is this a string that
   resolves to an installed locomotion plugin?", not "is this on the list?" (§4.3).
2. **The `drive` block carries an `RSV` `legs` sub-structure** (§4.2) — count, DoF per leg,
   gait, balance strategy. Present in the schema, ignored by the engine, exactly like the
   drift and consolidation fields. When a legged plugin arrives, no existing manifest changes.
3. **Locomotion is a plugin category** (§12.1). `differential` and `tracked` are the first
   two locomotion plugins, not hardwired engine logic. This is the actual doorframe: the
   engine asks a plugin how to move rather than knowing how to move.

The schema work is near-free. The biped itself is not, and the cheap schema decision must
not disguise that.

#### The assumption that makes it tractable — static walking

A tall biped is an inverted pendulum: falling over at all times, catching itself
continuously, requiring fast sensing and control on a hard real-time loop. That is the
research-project version, and Emet is not going to ship it.

**Emet's first-party biped target is deliberately the easy case: short legs, a low centre
of mass, and large feet.** What actually buys the simplification is precise, and worth
stating correctly rather than by slogan:

- **Large feet enlarge the support polygon**, so the centre of mass can stay above it
  throughout the gait. This is the condition for **static walking** — shift weight fully
  onto one foot, move the other, never enter a falling phase.
- **A low centre of mass makes disturbances forgiving.** Height is not itself
  disqualifying, but a tall body converts a small perturbation into a large moment and
  leaves less time to react, which is what forces fast control.
- **Slow, deliberate motion** keeps the gait quasi-static, so inertial effects stay
  negligible and no state estimator is needed.

Together these remove the dynamic balance controller and the high-rate state estimation.
They do not remove leg inverse kinematics, foot trajectory planning, servo coordination,
or a chassis stiff enough not to sag — so this is **weeks of work, not a weekend**. The
honest contrast is weeks against a multi-year control problem, and it is decided by the
*body*, not the software.

It reads as a small character walking deliberately rather than a human running. That is the
entire requirement.

`legs.gait: static` and `legs.balance: none` are therefore the defaults, and `dynamic` /
`imu_feedback` exist in the schema to keep the door open for someone else's harder robot.

**Recorded:** the biped body is designed around the walking model, not the other way around.
Whichever release ships legs inherits this constraint.

---

## 8. Soul Bundle

A portable directory. Moving it to a different body is the flagship demo: same memories, same personality, new form, and it notices.

```
emet.emet/                       # the reference soul; others: hugr.emet, neuma.emet
  soul.yaml           # P0  identity, persona, voice, behavior config
  memory.db           # P0  SQLite, see §9
  motion/
    default.pack.yaml # P0  recorded clips, see §10
  voice/              # RSV  bundled TTS model, or a reference
  wake/
    emet.ppn          # RSV  custom wake word (Porcupine, licensing pending)
  bundle.lock         # P0  engine version, schema version, checksum
```

### 8.1 `soul.yaml`

```yaml
bundle_version: "0.1"

identity:
  name: "Emet"                   # P0  what it is called. Free text, any name.
  wake_word: "emet"              # P0  which pretrained wake word wakes it.
                                 #     MUST be one of the shipped set (§14).
                                 #     Deliberately separate from `name` — see 8.1.1.
  line: emet                     # P0  emet | hugr | neuma | custom  (soul line, §1.2)
  pronouns: "it/its"             # P0
  author: "The Emet Authors"     # P0  free text; a handle, a name, anything
  created: "2026-08-08"          # P0
  license: null                  # RSV  for the community soul registry

voice:
  engine: piper                  # P0  piper | cloud
  model: "en_US-amy-medium"      # P0
  rate: 1.0                      # P0
  pitch_shift_semitones: 0       # P0

persona:
  summary: >                     # P0  the seed a human writes
    Earnest and plain-spoken. Deeply curious, honest to a fault, and a
    little too eager to tell you a true thing it just learned.
  system_prompt: |               # P0  compiled from summary, or hand-written
    ...
  traits:                        # P0  numeric knobs the engine actually reads
    warmth: 0.7
    humor: 0.4
    verbosity: 0.4
    curiosity: 0.9
    formality: 0.3
  drift:                         # RSV  see §11
    enabled: false
    rate: 0.0
    locked_traits: []

interaction:
  patience_ms: 900               # P0  silence threshold before responding
  extend_on_incomplete: true     # P0  one extra window on a trailing clause
  barge_in: true                 # P0  requires audio.input.aec != none
  backchannel: true              # P0  local micro-model filler
  greeting:
    on_recognized: true          # P0
    on_unknown: true             # P0

idle:                            # P0 (minimal), V1 (full)
  enabled: true
  interval_s: [20, 90]
  weights: {settle: 0.4, look_around: 0.4, fidget: 0.2}
  proactivity: 0.0               # RSV  0.0 = never initiates. See §11.

memory:
  personalization: true          # P0  master toggle, Gemini-style
  cross_person_disclosure: true  # P0  see §9.3
  consolidation: nightly         # RSV  nightly | off
  retention_days: 0              # P0   0 = forever

models:                          # P0  BYOK
  chat:  {provider: openai,   model: "...", key_env: "EMET_OPENAI_KEY"}
  stt:   {provider: deepgram, model: "...", key_env: "EMET_DEEPGRAM_KEY"}
  tts:   {provider: local_piper}
  micro: {provider: local, model: "qwen3-0.6b-q4"}   # backchannel only
```

#### 8.1.1 Why `name` and `wake_word` are separate fields

P0 ships a fixed set of pretrained wake words (§14) — custom wake words are `RSV`, pending licensing. If a single field served as both the robot's name and its wake word, then naming a soul "Barnaby" would silently produce a robot that cannot be woken, and the only fix once souls exist in the wild would be a schema migration.

Two fields, decided now, cost one line:

- **`name`** is free text. Call your robot anything. It appears in the persona, the greeting, and the docs.
- **`wake_word`** must be a member of the shipped set. The validator rejects anything else, naming the available options.

So a soul may be called Barnaby and answer to "Emet", and the persona can be told as much: *"Your name is Barnaby, but you only hear people when they say 'Emet' — you find this a little undignified."* Turning a P0 limitation into character is exactly the move principle 6 asks for: the constraint is real, so say so rather than hiding it.

When custom wake words arrive, `wake_word` simply accepts more values. No migration, and every existing soul keeps working.

### 8.2 The soul line field

`identity.line` names which reference personality a bundle derives from (§1.2). It is metadata only — the engine does not branch on it — but it lets the community registry group and filter souls, and it lets docs say "based on Hugr" meaningfully. Custom souls set `line: custom`.

### 8.3 Portability contract

A bundle is valid on any body. The engine **must not** write body-specific state into the bundle — no joint trims, no calibration, no device paths. Those live in `body.yaml`. If you ever want to store a servo offset in the soul, the abstraction has sprung a leak.

`bundle.lock` records the engine version that last wrote it. Loading a bundle written by a newer engine is a hard error; older is migrated forward on load, with a backup left beside it.

---

## 9. Memory schema

SQLite, one file, plainly readable. **Transparency is the actual privacy defense** — and it is principle 6: a soul named "truth" must keep readable records. The toggle is paperwork; a parent opening the file and reading it is real.

**Do not let this stand in for the whole privacy story.** A readable `memory.db` describes what Emet *retained*; it says nothing about what *left the house*. In P0 every utterance goes to a third-party STT provider and a third-party LLM provider (§14), and for most owners that is the larger fact. The cloud split is declared honestly here in the spec; the obligation is to declare it just as plainly at setup, where a non-technical owner will actually encounter it.

### 9.1 Tables — `P0`

```sql
CREATE TABLE people (
  id              TEXT PRIMARY KEY,
  display_name    TEXT NOT NULL,
  pronouns        TEXT,
  voiceprint      BLOB,            -- speaker-ID embedding
  enrolled_at     TEXT NOT NULL,
  last_seen_at    TEXT,
  profile         TEXT,            -- JSON, consolidated facts (RSV: rewritten nightly)
  is_owner        INTEGER DEFAULT 0
);

CREATE TABLE memories (
  id                TEXT PRIMARY KEY,
  created_at        TEXT NOT NULL,
  episode_id        TEXT REFERENCES episodes(id),

  source_person_id  TEXT REFERENCES people(id),   -- who said it
  subject_person_id TEXT REFERENCES people(id),   -- who it is ABOUT
                                                  -- NULL = about the world
                                                  -- '@self' = about the robot

  content           TEXT NOT NULL,                -- one fact, plain language
  kind              TEXT NOT NULL,   -- fact | event | preference | promise
                                     -- | feeling | self
  sensitivity       INTEGER NOT NULL DEFAULT 1,   -- §9.2
  confidence        REAL DEFAULT 1.0,
  salience          REAL DEFAULT 0.5,             -- decays; drives recall
  last_recalled_at  TEXT,
  superseded_by     TEXT REFERENCES memories(id)  -- never UPDATE, always supersede
);

CREATE TABLE episodes (
  id            TEXT PRIMARY KEY,
  started_at    TEXT NOT NULL,
  ended_at      TEXT,
  participants  TEXT,      -- JSON array of person ids
  summary       TEXT,      -- RSV: written by nightly consolidation
  transcript    TEXT       -- RSV: retention_days applies here first
);

CREATE INDEX idx_mem_subject ON memories(subject_person_id, salience DESC);
CREATE INDEX idx_mem_source  ON memories(source_person_id);
```

**Never `UPDATE` a memory.** Supersede it. You want the history of what the robot believed — for consolidation quality, and because "why does it think that?" is a question users will ask, and a truth-named product should be able to answer.

### 9.2 Sensitivity levels

| Level | Name | Engine behavior |
|---|---|---|
| 0 | `open` | Freely recalled with anyone present. |
| 1 | `personal` | Recalled when the subject is present. With others, gated on `cross_person_disclosure`. Default. |
| 2 | `private` | Recalled **only** when the source person is the sole participant. Mechanically enforced. |
| 3 | `sealed` | Never enters a prompt. Retained solely for user inspection and export. |

**The split that makes "alive, not a servant" workable:** levels 2 and 3 are enforced in the retrieval query — the soul never sees them, so it cannot leak them regardless of what it is talked into. Levels 0 and 1 are left to the character's judgment. Hard floor on the catastrophic cases; personality everywhere above it.

`P0`: the soul assigns sensitivity at write time via a tool call, defaulting to 1. `RSV`: consolidation re-scores overnight with a larger model.

### 9.3 The disclosure toggle

`cross_person_disclosure: true` — the robot may tell one person something it learned about another (level-0 and level-1 memories). This is the default because it is what a companion who *cared* would do, and it is the line between a companion and an appliance. Set `false` and level-1 becomes source-locked, matching personal-assistant expectations.

**`P0` requirements:** surfaced during setup in plain language, not buried; `emet memory export` produces readable JSON; `emet memory forget <id>` is honored immediately.

---

## 10. Motion packs

Tier 1 (declarative kinematics) and tier 3 (recorded clips) are `P0`. Tier 2 (user Python intent handlers) is `V1`, but the interface is shaped for it now.

```yaml
pack_version: "0.1"
name: "scout default"
requires:
  - {role: head, joints: [pan, tilt]}
clips:
  curious_tilt:
    intent: express.curiosity
    easing: ease_in_out
    keyframes:
      - {t_ms: 0,    joints: {pan: 0,  tilt: 0}}
      - {t_ms: 300,  joints: {pan: -8, tilt: 12}}
      - {t_ms: 1000, joints: {pan: -8, tilt: 12}}
      - {t_ms: 1400, joints: {pan: 0,  tilt: 0}}
```

`requires` is matched against the manifest; a pack whose requirements are unmet is skipped and the chain falls through to the next rung. Recording is `emet teach curious_tilt`: relax the servos, let the builder pose the robot, capture keyframes on a keypress. No code, no math, works on bodies you have never seen.

**`V1` — cloud motion generation:** POST the manifest plus `body.description` to a large model, receive a full pack keyed by intent, cache locally forever. How a builder with a tentacle gets expressive motion without writing a line. Depends on nothing in the schema changing — which is why the pack format is declarative data.

---

## 11. Idle, proactivity, and drift

### 11.1 Idle and proactivity

The subsystem that decides whether this is a character or a smart speaker in a costume. Architecturally a **separate loop** from conversation, at low priority.

**`P0` — idle only.** A timer fires on `idle.interval_s`, samples `idle.weights`, emits an `idle` intent. Preempted by anything. On a body with no actuators it is a silent no-op (verify it is silently correct, not crashing).

**`P0` — reactive attention.** Not proactivity, but reads as aliveness cheaply: a voice detected in the room with no wake word still triggers `attend`. The robot looks over when you speak near it. Local, cheap, large effect.

**`RSV` — `proactivity` scalar.** Reserved in `soul.yaml`, ignored in P0.

**`V1` — genuine initiation.** The robot starts conversations: a person recognized after an absence, an unresolved thread from a prior episode, a `promise`-kind memory coming due. Needs a salience pass over memory, a social-appropriateness model, and a *very* conservative rate limiter. Highest-risk feature in the product — a companion that talks unprompted is either magical or intolerable, and the difference is mostly frequency. Do not attempt before the conversation loop is boring and solid.

### 11.2 Drift

**`RSV` in schema, `V1` in behaviour.** Fields exist so nothing migrates later.

Constraint fixed now: drift must be **auditable and reversible.** Store trait deltas in a separate append-only table keyed to the episode that caused each change; never mutate `soul.yaml`. A user must see why their robot became quieter and be able to revert. Drift that silently rewrites the persona is unshippable.

---

## 12. Plugin interface

The public contract. Breaking it is a major version bump.

```python
from emet_sdk import ActuatorPlugin, CapabilityDescriptor, Action

class PCA9685Servos(ActuatorPlugin):
    capability_type = "joint_group"

    def describe(self) -> CapabilityDescriptor:
        """Report what this instance can actually do, post-init.
        The engine binds fallback chains against this, not the YAML."""

    async def apply(self, action: Action) -> None:
        """Execute. Must return promptly; long moves are driven by
        repeated apply() calls from the choreographer at 50Hz."""

    async def home(self) -> None: ...
    async def shutdown(self) -> None:
        """Called on SIGTERM and on fault. Must leave hardware safe."""

    def health(self) -> Health:
        """RSV — polled; feeds proprioceptive self-model updates."""
```

Sensors invert the flow: `SensorPlugin.poll()` returns typed readings the engine consumes; sensors never emit intents.

### 12.1 Locomotion plugins

Locomotion is its own plugin category, for the reason given in §7.1: the engine must not
contain "how to move" logic. It asks.

```python
class DifferentialDrive(LocomotionPlugin):
    kinematics = "differential"        # the string matched against drive.kinematics

    def describe(self) -> LocomotionDescriptor:
        """Degrees of freedom, achievable velocity envelope, whether the body
        can turn in place, whether it can move and turn simultaneously."""

    async def command(self, twist: Twist) -> None:
        """Accept a desired linear/angular velocity. The plugin owns everything
        below this line — wheel math, gait phase, balance, whatever it takes."""

    async def stop(self, hard: bool = False) -> None: ...
```

The engine emits `move` intents; the choreographer converts them to a `Twist`; the
locomotion plugin decides what that means for its body. Wheels solve it with arithmetic.
Treads solve it with the same arithmetic and different slip assumptions. A legged plugin
solves it with a gait generator, and the engine above it does not know or care.

**`P0`:** `emet_hal.differential` and `emet_hal.tracked` ship built in.
**Later:** `omni`, `ackermann`, `legged` — new packages, no schema change, no engine change.

`describe()` matters more here than for actuators: a body that cannot turn in place needs a
different `move.turn_to` realization than one that can, and the self-model (§7) needs to say
so honestly.

**`P0`:** plugins are ordinary Python packages, trusted, in-process. Discovery via entry points.
**`V1`:** a curated registry, signing, then sandboxing. With an open engine the primary defense is provenance and review rather than isolation. Necessary before accepting third-party plugins from strangers at volume; unnecessary while users are people who already trust you.

**Capability tiers (from earlier decision):**
- **Tier 1 — declarative** (`P0`): wheels, treads, pan/tilt joints. No user code.
- **Tier 3 — recorded** (`P0`): `emet teach`. No user code.
- **Tier 2 — user Python** (`V1`): custom intent handlers for exotic bodies, behind the sandbox.

---

## 13. Turn-taking

Four decisions, defaults chosen. All `P0` — turn-taking is what separates charming from infuriating, and no personality prompting fixes it.

1. **Endpoint:** silence threshold, default `patience_ms: 900`, exposed as a persona trait so a thoughtful soul (Neuma) waits longer than an eager one (Hugr).
2. **Barge-in:** on. Speech during playback stops audio *mid-word* and starts listening. Requires mic-array AEC.
3. **Thinking gap:** local micro-model backchannel within 300ms, so a sound arrives before the cloud answer.
4. **Trailing clause:** if the transcript looks incomplete, extend the silence window once (`extend_on_incomplete`). Cheap heuristic, big perceived-intelligence payoff.

---

## 14. Cloud vs local split

| Stage | Placement | Note |
|---|---|---|
| Wake word | Local | Fixed pretrained set in P0; custom (Porcupine) is `RSV`, pending licensing. |
| VAD, speaker ID | Local | Reflex tier. |
| Gaze / DoA / face tracking | Local | Reflex tier. |
| STT | Cloud (streaming) | BYOK. |
| LLM | Cloud (streaming) | BYOK. Speak first sentence as it streams. |
| TTS | Local (Piper) | The expensive cloud stage; keep it local. Premium cloud voice is an opt-in toggle. |
| Backchannel micro-model | Local | Fills the thinking gap. |
| Vision understanding | Cloud (VLM), event-triggered | One frame on an explicit trigger. Never streamed. |
| Nightly consolidation | Cloud (batch) | `RSV`. Nobody waiting; use the biggest model. |
| Persona / motion compilation | Cloud (one-shot) | `RSV`/`V1`. Offline after caching. |

Online-first. Offline degraded mode (a local small model taking over) is `V1`; in P0 the robot fails **loudly and in character** when the network or a key is missing.

---
