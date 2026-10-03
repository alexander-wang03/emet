#!/usr/bin/env bash
# Prompts for recording one-breath questions: the name and the question with
# no pause between them, the way people talk to a speaker they are used to.
# The recording is what the cut before the wake is tuned against. On 0.5.1,
# three of ten one-breath "hey emet, what's two plus two" came back from the
# transcriber as "It was two plus two." (2026-09-27). Run it beside arecord,
# in the same terminal:
#
#   bash tools/one_breath_prompts.sh & arecord -D plughw:3,0 -f S16_LE -r 16000 -c 1 -d 130 ~/one-breath.wav
#
# 20 prompts, one every six seconds after a five-second lead, so 125 s of
# prompts inside the 130 s recording. The questions start with different
# first sounds (a vowel, a fricative, a stop, a glide, a nasal), because the
# first word is what the cut can clip, and two are a single word. Say each
# prompt in one breath, at a normal distance and volume, and leave the
# room's ordinary noise in.
#
# Every clip made for --replay needs at least 5 s of room before its first
# phrase, and the lead gives it that. The wake decoder first updates its
# cepstral mean about 3 s in, and a phrase said before then is heard against
# the mean the replay started from: on 0.5, a clip with the phrase 1.3 s in
# woke nothing on the reference body (2026-09-24).
#
# Replay the recording with the mock transcriber, which costs nothing, to
# count its wakes before any paid replay, whose transcripts are what the
# tuning reads. The first recording made with this script replayed to 10
# wakes of 20, on the laptop and on the reference body (2026-09-29 and 30),
# none of them on "what's two plus two". To record that question, or any one
# question, on its own, name it in ONE_BREATH_ONLY. It is asked
# ONE_BREATH_TIMES times, 10 unless set, on the same lead and interval:
#
#   ONE_BREATH_ONLY="what's two plus two" bash tools/one_breath_prompts.sh & arecord -D plughw:3,0 -f S16_LE -r 16000 -c 1 -d 70 ~/two-plus-two.wav
#
# 10 prompts, so 65 s of prompts inside the 70 s recording. With another
# ONE_BREATH_TIMES, -d is the 5 s lead, 6 s a prompt and 5 s to spare.
#
# ONE_BREATH_INTERVAL and ONE_BREATH_LEAD (seconds) exist so the script can
# be checked quickly without waiting two minutes.

set -u

INTERVAL="${ONE_BREATH_INTERVAL:-6}"
LEAD="${ONE_BREATH_LEAD:-5}"
ONLY="${ONE_BREATH_ONLY:-}"
TIMES="${ONE_BREATH_TIMES:-10}"

QUESTIONS=(
  "what's two plus two"
  "where are my keys"
  "is it going to rain today"
  "can you hear me"
  "how far away is the moon"
  "tell me a joke"
  "why is the sky blue"
  "what time is it"
  "are you awake"
  "should I bring an umbrella"
  "yes"
  "no"
  "open the window"
  "give me a fact about octopuses"
  "which way is north"
  "do you remember me"
  "play something quiet"
  "make it louder"
  "every day or every week"
  "what did I just say"
)

# ONE_BREATH_TIMES alone would print the set of 20 into a recording sized for
# one question. A count bash reads as zero, as octal (010 is 8) or wrapped
# (2^63 reads as negative) would print no prompts or the wrong number while
# arecord records the room. 999, the most it takes, is 100 minutes of prompts.
if [[ -z "$ONLY" && -n "${ONE_BREATH_TIMES:-}" ]]; then
  printf 'one_breath_prompts: ONE_BREATH_TIMES needs ONE_BREATH_ONLY, the question it repeats\n' >&2
  exit 2
fi
if [[ ! "$TIMES" =~ ^[1-9][0-9]{0,2}$ ]]; then
  printf "one_breath_prompts: ONE_BREATH_TIMES is '%s'; give a whole number from 1 to 999, with no leading zero\n" "$TIMES" >&2
  exit 2
fi

if [[ -n "$ONLY" ]]; then
  PROMPTS=()
  for ((n = 0; n < TIMES; n++)); do
    PROMPTS+=("$ONLY")
  done
else
  PROMPTS=("${QUESTIONS[@]}")
fi

sleep "$LEAD"
for i in "${!PROMPTS[@]}"; do
  printf '%2d  say in one breath: "hey emet, %s"\n' "$((i + 1))" "${PROMPTS[$i]}"
  sleep "$INTERVAL"
done
printf '\ndone: %d one-breath question(s). Count the wakes with the mock transcriber before any paid replay.\n' "${#PROMPTS[@]}"
