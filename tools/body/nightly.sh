#!/bin/bash
# Run master's suites, checks and bank replays on the reference body each
# night with nobody there (P11), and leave a dated report that
# `python tools/body/run.py --release X.Y.Z --nightly-report` reads home.
#
# Installed as ~/emet-ci/bin/nightly.sh and run by the body's cron each night
# (tools/body/README.md, "The nightly", has the crontab line). It:
#
# - gives way exactly as run.py's preflight does: it does nothing but write a
#   report saying why while an emet-listen, emet-talk, arecord, aplay or the
#   bank's record.py runs, while a sound card's PCM is open, or while
#   ~/emet-ci/lock is held, a soak's included. That report has a name of its
#   own, YYYY-MM-DD.gave-way.txt, and the day's report and folder are left
#   as they are;
# - takes ~/emet-ci/lock with a token of its own before it touches anything,
#   and frees it only when the token inside is still its own;
# - fetches the public repository anonymously into the runner's bare clone,
#   ~/emet-ci/repo.git, and swaps master into ~/emet-ci/emet, which is the
#   tree the runner's venv is installed from; the record in
#   ~/emet-ci/source.json then says "nightly", so a later run.py that does
#   not sync names what it ran on. No GitHub credential is needed or read;
# - installs with run.py's install line when a pyproject.toml changed, and
#   runs the four suites, the checks and tools/bank/replay.py over
#   ~/voicebank with the runner's venv. None of them opens an audio device:
#   the tests declare the null sink, the checks read files, and replay.py
#   replays into the null sink. The PCM check runs again at the end and its
#   lines go in the report;
# - unsets every EMET_ variable first, so no key reaches a run;
# - keeps the newest 30 reports.
#
# Each report is ~/emet-ci/nightly/YYYY-MM-DD.txt, or YYYY-MM-DD.gave-way.txt
# for a night it gave way, `key=value` lines: format, started, ended, verdict
# (passed, failed, gave way or not done), reason, head, subject, installed,
# suite_<package>, checks, bank, note, pcm_after.

set -u
umask 022
PATH=/usr/local/bin:/usr/bin:/bin
export PATH

CI="$HOME/emet-ci"
PY="$CI/.venv/bin/python"
UPSTREAM="https://github.com/alexander-wang03/emet"
NIGHTLY="$CI/nightly"
KEEP=30
BANK="$HOME/voicebank"
# Where the kernel lists the sound cards' PCMs; a test points it elsewhere.
ASOUND=/proc/asound
# run.py's INSTALL, joined by spaces: the same line gives the same install
# key, so a nightly and a run.py never reinstall for each other.
EXTRAS="emet-sdk[dev] emet-hal[dev,audio,wake] emet-providers[dev,deepgram,anthropic,openai,piper] emet-engine[dev]"
PACKAGES="emet-sdk emet-hal emet-providers emet-engine"

# run.py's BUSY, as an extended regular expression: the programs that mean
# the robot is in use, matched in `ps`'s argument lists.
BUSY_ERE='(^|[[:space:]/])(emet-listen|emet-talk|arecord|aplay)([[:space:]]|$)|(^|[^[:alnum:]_])emet_engine\.(cli|talk)([^[:alnum:]_]|$)|(^|[[:space:]/])tools/bank/record\.py([^[:alnum:]_]|$)'

busy_lines() {
  # stdin: `ps -eo pid=,args=`; stdout: the lines that mean the robot is in use.
  grep -E "$BUSY_ERE" || true
}

pcm_open() {
  # run.py's PCM_CHECK: each PCM substream whose status is other than closed.
  local root="${1:-$ASOUND}" f first
  for f in "$root"/card*/pcm*/sub*/status; do
    [ -r "$f" ] || continue
    first=$(head -n 1 "$f")
    [ "$first" = "closed" ] || echo "pcm=$f: $first, $(grep -m 1 owner_pid "$f" | tr -s ' ')"
  done
}

say() {
  # One `key=value` line of the report being written.
  printf '%s=%s\n' "$1" "$2" >> "$PART"
}

prune() {
  # The newest KEEP reports stay, with the folder of each. A report is
  # YYYY-MM-DD.txt or YYYY-MM-DD.gave-way.txt, which the two globs name, so
  # any other file in the folder stays.
  local f old
  for f in "$NIGHTLY"/[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].txt \
           "$NIGHTLY"/[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].gave-way.txt; do
    [ -e "$f" ] && printf '%s\n' "${f##*/}"
  done | sort | head -n "-$KEEP" |
    while IFS= read -r old; do
      rm -f "$NIGHTLY/$old"
      rm -rf "${NIGHTLY:?}/${old%.txt}"
    done
}

gate() {
  # Why the nightly gives way now, or nothing.
  local ps busy pcm
  ps=$(ps -eo pid=,args= 2>/dev/null)
  busy=$(printf '%s\n' "$ps" | busy_lines | head -n 3 | tr -s ' ' | tr '\n' ';')
  if [ -n "$busy" ]; then echo "the robot is in use: $busy"; return; fi
  pcm=$(pcm_open | head -n 3 | tr '\n' ';')
  if [ -n "$pcm" ]; then echo "a sound card is open: $pcm"; return; fi
  # The tilde is text for a person, in run.py's own words for a held lock.
  # shellcheck disable=SC2088
  if [ -e "$CI/lock" ]; then echo "~/emet-ci/lock is held: $(tr '\n' ' ' < "$CI/lock")"; return; fi
}

TOKEN=""
take_lock() {
  TOKEN="nightly-$(head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  mkdir -p "$CI"
  ( set -C; printf 'token %s\nby tools/body/nightly.sh\nstarted %s\n' "$TOKEN" "$(date -Iseconds)" > "$CI/lock" ) 2>/dev/null
}

release_lock() {
  if [ -n "$TOKEN" ] && grep -qx "token $TOKEN" "$CI/lock" 2>/dev/null; then rm -f "$CI/lock"; fi
}

finish() {
  # The report, whole or as far as it got, put in place in one move.
  local verdict="$1" reason="$2"
  say verdict "$verdict"
  [ -n "$reason" ] && say reason "$reason"
  say ended "$(date -Iseconds)"
  mv -f "$PART" "$REPORT"
  release_lock
  prune
}

sync_master() {
  # Fetch master anonymously and swap it into the runner's tree, as
  # run.py --commit does. Prints `head=SHA` and `subject=...`.
  local git="git -C $CI/repo.git" sha
  mkdir -p "$CI"
  [ -d "$CI/repo.git" ] || git init -q --bare "$CI/repo.git" || return 1
  GIT_TERMINAL_PROMPT=0 $git -c credential.helper= fetch -q --prune --no-tags "$UPSTREAM" \
    '+refs/heads/*:refs/remotes/origin/*' '+refs/tags/*:refs/tags/*' || return 1
  sha=$($git rev-parse --verify --quiet 'refs/remotes/origin/master^{commit}') || return 1
  rm -rf "$CI/incoming" && mkdir -p "$CI/incoming" || return 1
  $git archive --format=tar "$sha" | tar -x -C "$CI/incoming" || return 1
  rm -rf "$CI/emet" && mv "$CI/incoming" "$CI/emet" || return 1
  say head "$sha"
  say subject "$($git log -1 --format=%s "$sha")"
  python3 -c 'import json, sys; print(json.dumps({"kind": "nightly", "ref": "master", "head": sys.argv[1], "subject": sys.argv[2], "upstream": sys.argv[3]}, sort_keys=True))' \
    "$sha" "$($git log -1 --format=%s "$sha")" "$UPSTREAM" > "$CI/source.json"
}

install_tree() {
  # run.py's install step: again only when a pyproject.toml or the line changed.
  local key args="" extra
  cd "$CI/emet" || return 1
  key=$( { printf '%s\n' "$EXTRAS"; for p in $PACKAGES; do cat "$p/pyproject.toml"; done; } | sha256sum | cut -c1-64)
  if [ ! -x "$PY" ]; then python3 -m venv "$CI/.venv" || return 1; fi
  if [ "$(cat "$CI/installed.key" 2>/dev/null)" != "$key" ]; then
    rm -f "$CI/installed.key"
    for extra in $EXTRAS; do args="$args -e $extra"; done
    # shellcheck disable=SC2086
    "$PY" -m pip install --disable-pip-version-check -q $args > "$RUN/install.txt" 2>&1 || return 1
    echo "$key" > "$CI/installed.key"
    say installed yes
  else
    say installed no
  fi
}

suites() {
  local p code failed=0
  for p in $PACKAGES; do
    (cd "$CI/emet/$p" && timeout 1800 "$PY" -m pytest -q -p no:cacheprovider > "$RUN/suite-$p.txt" 2>&1)
    code=$?
    say "suite_$p" "code $code; $(tail -n 1 "$RUN/suite-$p.txt")"
    [ "$code" -eq 0 ] || failed=1
  done
  return "$failed"
}

checks() {
  local c code said="" failed=0
  cd "$CI/emet" || return 1
  if [ -f tools/ci.py ]; then
    "$PY" tools/ci.py layering > "$RUN/check-ci-layering.txt" 2>&1
    code=$?
    say checks "tools/ci.py layering exit $code: $(tail -n 1 "$RUN/check-ci-layering.txt")"
    return "$code"
  fi
  for c in check_layering release_check check_style; do
    "$PY" "tools/$c.py" . > "$RUN/check-$c.txt" 2>&1
    code=$?
    said="$said$c $code; "
    [ "$code" -eq 0 ] || failed=1
  done
  say checks "${said%; }"
  return "$failed"
}

bank() {
  local code held search=""
  cd "$CI/emet" || return 1
  [ -f "$BANK/bank.json" ] || { say bank "not run: there is no bank at $BANK"; return 0; }
  [ -d "$BANK/audio" ] && search="--search $BANK/audio"
  # shellcheck disable=SC2086
  timeout 3600 "$PY" tools/bank/replay.py "$BANK" $search --report "$RUN/bank-report.json" > "$RUN/bank-replay.txt" 2>&1
  code=$?
  held=$("$PY" -c 'import json, sys; r = json.load(open(sys.argv[1])); h = r.get("held"); print(len(h) if isinstance(h, list) else "unknown")' "$RUN/bank-report.json" 2>/dev/null || echo unknown)
  say bank "code $code; held $held; $(tail -n 1 "$RUN/bank-replay.txt")"
  # As run.py's bank step: a replay that held no recording to a golden fails.
  [ "$code" -eq 0 ] && [ "$held" != "unknown" ] && [ "$held" != "0" ]
}

main() {
  for name in $(env | sed -n 's/^\(EMET_[A-Za-z0-9_]*\)=.*/\1/p'); do unset "$name"; done
  export PYTHONDONTWRITEBYTECODE=1 GIT_TERMINAL_PROMPT=0 TMPDIR="$CI/tmp" PIP_CACHE_DIR="$CI/cache/pip"
  mkdir -p "$TMPDIR" "$NIGHTLY"
  local day why began failed=0
  day=$(date +%Y-%m-%d)
  began=$(date -Iseconds)
  why=$(gate)
  # As in gate(), the tilde is text for a person.
  # shellcheck disable=SC2088
  if [ -z "$why" ] && ! take_lock; then why="~/emet-ci/lock was taken as the nightly began"; fi
  if [ -n "$why" ]; then
    # Without the lock nothing of the day's run is touched: another nightly
    # may be running it, or may have finished it.
    REPORT="$NIGHTLY/$day.gave-way.txt"
    PART="$NIGHTLY/.$day.gave-way.$$.part"
    say format "emet-nightly/1"
    say started "$began"
    finish "gave way" "$why"
    return 0
  fi
  # A nightly stopped part way frees its own lock all the same.
  trap release_lock EXIT
  trap 'exit 143' TERM INT
  REPORT="$NIGHTLY/$day.txt"
  PART="$NIGHTLY/.$day.txt.part"
  RUN="$NIGHTLY/$day"
  rm -f "$PART"
  rm -rf "$RUN" && mkdir -p "$RUN"
  say format "emet-nightly/1"
  say started "$began"
  if ! sync_master; then finish "not done" "master could not be fetched from $UPSTREAM"; return 1; fi
  if ! install_tree; then finish "not done" "the install failed ($RUN/install.txt)"; return 1; fi
  if [ -f "$CI/emet/tools/body/nightly.sh" ] && ! cmp -s "$0" "$CI/emet/tools/body/nightly.sh"; then
    say note "this nightly.sh is not master's tools/body/nightly.sh: install it again (tools/body/README.md)"
  fi
  suites || failed=1
  checks || failed=1
  bank || failed=1
  say pcm_after "$(pcm_open | tr '\n' ';')"
  if [ "$failed" -eq 0 ]; then finish passed ""; else finish failed "a suite, a check or the bank failed"; fi
  return "$failed"
}

# Sourced with NIGHTLY_SOURCE_ONLY=1, the functions are defined and nothing
# runs: the tests call them one at a time.
if [ "${NIGHTLY_SOURCE_ONLY:-}" != "1" ]; then
  main "$@"
fi
