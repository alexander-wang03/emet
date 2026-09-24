## What this changes

<!-- One or two sentences. The PR title becomes the commit on master, as
     `X.Y.Z: What it does`. The part after the colon is also the first line
     of this version's CHANGELOG.md entry. -->

## Why

<!-- What problem this solves, or what it makes possible. -->

---

### Checklist

- [ ] Commits are signed off (`git commit -s`). See CONTRIBUTING.md.
- [ ] `python tools/check_layering.py .` and `python tools/release_check.py .` pass.
- [ ] `pytest` passes in `emet-sdk`, `emet-hal`, `emet-providers` and `emet-engine`.
- [ ] The version is bumped in the four `pyproject.toml` files and `CHANGELOG.md`
      has the entry, so the merge can be tagged.
- [ ] New behaviour has a test and a caller.
- [ ] Anything taken from a paper or repository is cited at the point of use
      and in CITATIONS.md.

### If this touches the contract layer

Schemas, `types.py`, `intents.py`, `plugin.py`, or `chains.py`. Leave blank if
it does not.

- [ ] This was discussed in an issue first.
- [ ] Existing manifests and soul bundles still validate.
- [ ] New schema fields are tagged `P0`, `RSV`, or `V1` in DESIGN.md.

### Design rules

These are invariants rather than preferences. A change that breaks one is wrong
even when it works. See DESIGN.md section 2.

- [ ] The soul names no hardware.
- [ ] Every fallback chain still terminates in a voice rung.
- [ ] `emet_sdk` imports nothing internal; `emet_hal`, `emet_providers` and
      `emet_engine` import `emet_sdk` only.
- [ ] Memory is not namespaced by body.
- [ ] A missing plugin is still distinct from a schema error.
