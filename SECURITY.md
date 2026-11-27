# Security

## Reporting a vulnerability

**Do not open a public issue.**

Use [GitHub's private vulnerability reporting](https://github.com/alexander-wang03/emet/security/advisories/new)
on this repository, the **Report a vulnerability** form. It creates a private
thread that only you and the maintainer can see.

If GitHub does not offer you the form, or you would rather use email, write
to <wake.up.emet@gmail.com>.

## What follows a report

Emet has one maintainer, so every time below is one a single person can keep.

1. **A first response within a week.** It says the report was read and what
   happens next. If a week passes with no answer, the report has gone
   astray, so please send it again by the other route.
2. **The report stays private while it is fixed, for 90 days at most.** The
   90 days run from the day you sent it. By then the advisory is published:
   with the fix, or, if there is no fix yet, with what an owner can do in
   the meantime. Please keep the details to yourself until it is.
3. **A CVE id is requested for a confirmed vulnerability in a released
   version.** The request goes through the private advisory to GitHub,
   which is a CVE Numbering Authority. GitHub decides whether to assign one.
4. **The fix goes out as a release, and the advisory is published with it.**
   Every merge to `master` is a version with its own tag, so a fix is a
   patch release. The advisory names the versions affected, the version
   that fixes it, and what an owner should do.
5. **The changelog names it.** The release's entry in
   [CHANGELOG.md](CHANGELOG.md) has a `### Security` section, and each line
   of it carries the advisory's id: `GHSA-xxxx-xxxx-xxxx`, with the CVE id
   when one was assigned. `tools/release_check.py` refuses an entry whose
   Security line names no id. A release that only raises a dependency past
   that dependency's own advisory names that advisory the same way.
6. **You are credited in the advisory,** unless you ask not to be.

The 90-day limit, a CVE request for each confirmed vulnerability and the
credit follow the Zephyr Project's vulnerability reporting process
([CITATIONS.md](CITATIONS.md)). The `Security` section is
Keep a Changelog's, the format the changelog already follows.

## What counts as a vulnerability here

Emet is not a web service, so the usual categories translate oddly. The things
that matter for a companion robot are:

**Anything that leaks memory.** Emet does not remember yet: memory is planned
for 0.6. Its database will hold what people said in their homes, including
things they said about other people. In the design (`DESIGN.md` §9.2), the
retrieval query itself will enforce sensitivity levels 2 and 3, so that the
personality never receives a memory it may not repeat and cannot be talked
into repeating one. Nothing enforces that today, because nothing is stored.
From the release that stores memories, a conversation that draws out a
level-3 memory, or a level-2 memory while anyone but the person who shared
it is present, is a vulnerability.

**Anything that makes the robot lie about what it is doing.** A path that lets
the camera capture without the robot saying so, or that reports a capability
the body does not have, breaks the guarantee the project is named for.

**Anything that moves hardware unexpectedly.** A manifest declares joint
ranges, `max_continuous_motion_s`, a thermal limit and an emergency stop, to
guard a physical object that may be near a person. Today nothing enforces
them while a body moves: joint ranges are checked when a manifest is
validated, and no code reads the `safety` block. Emet ships no driver for a
real motor yet, but the engine does drive a third-party actuator plugin, with
none of these limits applied. Enforcing them is planned for 1.0. Until then a
limit that is not enforced is a known gap, and from 1.0 a way around one is a
vulnerability. Hardware that moves when no intent asked for it is a
vulnerability today.

**The usual software issues**: code execution through a crafted manifest, soul
bundle, or motion pack; credential leakage; dependency vulnerabilities; and
workflow or supply-chain weaknesses in this repository.

## What does not count

**Third-party plugins running with full access.** Installing a plugin runs that
person's code on your robot, with everything the robot has. This is documented
in `CONTRIBUTING.md` and is a property of the design at this stage. A signed
registry with reviewed provenance is planned. Until then, install plugins from
people you trust.

**Anything requiring physical access to the device.** Someone holding your
robot can read its SD card. The memory database, when it arrives, will be
plain SQLite by design, so that its owner can read it too.

**Cloud provider behaviour.** Emet sends speech and text to whichever providers
you configure. Their handling of that data is between you and them, and is one
reason the project intends to move inference on-device.

## Supported versions

Before 1.0, only the latest release is supported. A fix merges to `master` as
the next patch release, with its own tag, and nothing is backported to an
earlier version.

From 1.0 this section will list the versions receiving fixes, because robots in
the field will be running them.

## Credentials

API keys are never stored in a soul bundle. `models.*.key_env` holds the *name*
of an environment variable, so a bundle can be published without carrying a
secret. A change that puts a key in a file is a bug worth reporting.
