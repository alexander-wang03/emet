# Security

## Reporting a vulnerability

**Do not open a public issue.**

Use [GitHub's private vulnerability reporting](https://github.com/alexander-wang03/emet/security/advisories/new)
on this repository. It creates a private thread visible only to maintainers.

If you would rather use email, write to <wake.up.emet@gmail.com>.

You should get a first response within a week. If you do not, the report has
gone astray rather than been ignored, so please chase it.

## What counts as a vulnerability here

Emet is not a web service, so the usual categories translate oddly. The things
that matter for a companion robot are:

**Anything that leaks memory.** The memory database holds what people said in
their homes, including things they said about other people. Sensitivity levels
2 and 3 are enforced in the retrieval query so that the personality never
receives them and therefore cannot be talked into repeating them. A way to
retrieve a level-2 or level-3 memory through conversation is a vulnerability,
not a bug.

**Anything that makes the robot lie about what it is doing.** A path that lets
the camera capture without the robot saying so, or that reports a capability
the body does not have, breaks the guarantee the project is named for.

**Anything that moves hardware unexpectedly.** Bypassing declared joint limits,
`max_continuous_motion_s`, or thermal limits. These guard a physical object
that may be near a person.

**The usual software issues**: code execution through a crafted manifest, soul
bundle, or motion pack; credential leakage; dependency vulnerabilities; and
workflow or supply-chain weaknesses in this repository.

## What does not count

**Third-party plugins running with full access.** Installing a plugin runs that
person's code on your robot, with everything the robot has. This is documented
in `CONTRIBUTING.md` and is a property of the design at this stage, not a
defect. A signed registry with reviewed provenance is planned. Until then,
install plugins from people you trust.

**Anything requiring physical access to the device.** Someone holding your
robot can read its SD card. The memory database is deliberately plain SQLite so
that its owner can read it too.

**Cloud provider behaviour.** Emet sends speech and text to whichever providers
you configure. Their handling of that data is between you and them, and is one
reason the project intends to move inference on-device.

## Supported versions

Pre-1.0, only the latest release is supported. Fixes land on `master` and go
out in the next release.

From 1.0 this section will list the versions receiving fixes, because robots in
the field will be running them.

## Credentials

API keys are never stored in a soul bundle. `models.*.key_env` holds the *name*
of an environment variable, so a bundle can be published without carrying a
secret. A change that puts a key in a file is a bug worth reporting.
