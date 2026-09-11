#!/usr/bin/env bash
# Prompts for recording the ten-minute soak file, so that two recordings made
# months apart exercise the detector the same way. Run it beside arecord, in
# the same terminal:
#
#   bash tools/soak_prompts.sh & arecord -D plughw:3,0 -f S16_LE -r 16000 -c 1 -d 600 ~/soak-10min.wav
#
# 58 prompts, one every ten seconds, the cadence of the x86 baseline. Every
# sixth prompt is a decoy that must not wake the robot, which fixes the tally:
# 49 wake phrases and 9 decoys. In the replay report, wakes above 49 are false
# fires and wakes below 49 are misses. Speak at a normal distance and volume,
# and leave the room's ordinary noise in.
#
# SOAK_INTERVAL and SOAK_LEAD (seconds) exist so the script can be checked
# quickly without waiting ten minutes.

set -u

INTERVAL="${SOAK_INTERVAL:-10}"
LEAD="${SOAK_LEAD:-3}"
COUNT=58

SENTENCES=(
  "what time is it"
  "remind me to call my sister tomorrow morning"
  "how far away is the moon"
  "I think the kitchen tap is dripping again"
  "play something quiet"
  "did anyone come to the door while I was out"
  "what is the capital of Mongolia"
  "I am going to bed early tonight"
  "tell me a fact about octopuses"
  "the meeting moved to three o'clock on Thursday"
  "turn the lights down a bit"
  "how do you spell necessary"
  "I left my keys somewhere in this room and I cannot find them anywhere, could you keep an eye out for them"
  "never mind"
)

DECOYS=(
  "hey emma, are you still there"
  "hey everyone, dinner is ready"
  "hey, meet me outside in five minutes"
  "there is a mess of cables on the desk"
  "hey Emmett, how was school"
  "the weather is meant to turn tomorrow"
  "hey, met any interesting people today"
  "I said no, and I meant it"
  "hey Emily, pass me the salt"
)

sleep "$LEAD"
wakes=0
decoys=0
for i in $(seq 1 "$COUNT"); do
  if (( i % 6 == 0 )); then
    decoys=$((decoys + 1))
    line="${DECOYS[$(( (decoys - 1) % ${#DECOYS[@]} ))]}"
    printf '%2d  DECOY, say only:   "%s"\n' "$i" "$line"
  else
    wakes=$((wakes + 1))
    line="${SENTENCES[$(( (wakes - 1) % ${#SENTENCES[@]} ))]}"
    printf '%2d  say: "hey emet"  (short pause)  "%s"\n' "$i" "$line"
  fi
  sleep "$INTERVAL"
done
printf '\ndone: %d wake phrases, %d decoys. Expect wakes = %d in the replay report.\n' "$wakes" "$decoys" "$wakes"
