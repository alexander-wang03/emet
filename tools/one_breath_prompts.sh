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
# 20 prompts, one every six seconds after a three-second lead, so 123 s of
# prompts inside the 130 s recording. The questions start with different
# first sounds (a vowel, a fricative, a stop, a glide, a nasal), because the
# first word is what the cut can clip, and two are a single word. Say each
# prompt in one breath, at a normal distance and volume, and leave the
# room's ordinary noise in. In the replay report, expect 20 wakes; the
# transcripts are what the tuning reads.
#
# ONE_BREATH_INTERVAL and ONE_BREATH_LEAD (seconds) exist so the script can
# be checked quickly without waiting two minutes.

set -u

INTERVAL="${ONE_BREATH_INTERVAL:-6}"
LEAD="${ONE_BREATH_LEAD:-3}"

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

sleep "$LEAD"
for i in "${!QUESTIONS[@]}"; do
  printf '%2d  say in one breath: "hey emet, %s"\n' "$((i + 1))" "${QUESTIONS[$i]}"
  sleep "$INTERVAL"
done
printf '\ndone: %d one-breath questions. Expect %d wakes in the replay report.\n' "${#QUESTIONS[@]}" "${#QUESTIONS[@]}"
