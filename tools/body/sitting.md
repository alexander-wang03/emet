# The release sitting

About 15 minutes at the robot, once a release, at the end. Ten turns of
`emet-talk`, judged by you against the sheet below. Everything else the body
has to show, the agent has already run over SSH with `tools/body/run.py`:
the suites, the voice bank's replays, and the speaker and microphone steps.
This sitting is the part only a person can judge: whether a real
conversation works.

Print this page. Read it standing at the robot.

## Before you start

- The agent's reply names the release's version, branch and commit, and
  gives the commands below with them filled in. Use those.
- A quiet room: the TV and the radio off. Stand where you usually talk to
  it. Leave the speaker's volume as it is.
- The log holds what the screen shows: your questions as heard and its
  answers. No audio is kept. Deepgram hears each turn and OpenAI reads it,
  as in any `emet-talk` run, through the keys in `~/.config/emet/keys.env`.

## 1. On the Pi: the release's code

Run the block whole.

```sh
cd ~/Documents/emet && source .venv/bin/activate && git fetch && git checkout BRANCH && git pull --ff-only && git log -1 --oneline
```

The last line is the commit the agent named. If the agent says a
`pyproject.toml` changed, reinstall before going on:

```sh
cd ~/Documents/emet && source .venv/bin/activate && pip install -e 'emet-sdk[dev]' -e 'emet-hal[dev,audio,wake]' -e 'emet-providers[dev,deepgram,openai,piper]' -e 'emet-engine[dev]'
```

## 2. Turns 1 and 2, with no flags

```sh
cd ~/Documents/emet && source .venv/bin/activate && script -qa ~/sitting-X.Y.Z.log -c "emet-talk ~/pi-speakerphone.yaml emet-sdk/examples/emet-soul.yaml"
```

Wait for `(ctrl-c to stop)`. Say turns 1 and 2. Press Ctrl-C once and wait
for the shell prompt to come back: after `stopped.` it still writes the
state file and a note.

## 3. Turns 3 to 10, with `--stats`

```sh
cd ~/Documents/emet && source .venv/bin/activate && script -qa ~/sitting-X.Y.Z.log -c "emet-talk ~/pi-speakerphone.yaml emet-sdk/examples/emet-soul.yaml --stats"
```

Wait for `(ctrl-c to stop)`. Say turns 3 to 10. Press Ctrl-C once and wait
for the shell prompt to come back: the footer, the `state: kept` line and
any note come first.

Both runs go into the one log; `-a` adds to it.

## The turns

Wait for each answer to finish before the next turn. After each turn, tick
the sheet.

| # | Stand | Say | What it shows |
|---|---|---|---|
| 1 | near | "Hey emet." Pause a moment. "What time is it?" | the name, a pause, a question |
| 2 | near | "Hey emet, what's seven times eight?" in one breath | a question in the same breath as the name; a number |
| 3 | near | "Hey emet, how far away is the moon?" in one breath | a question that starts with a weak "h" |
| 4 | near | "Hey emet, what's the tallest mountain in" and stop for about one second, then "Africa?" | the trailing clause: it waits through the pause |
| 5 | near | "Hey emet, can you come here?" | what it knows about its body: it cannot come, since it has no wheels |
| 6 | about 3 m | "Hey emet, who are you?" | the name from across the room |
| 7 | about 3 m | "Hey emet, thank you." | a short turn from across the room |
| 8 | near | "Hey emet, set a timer for fifteen minutes." | a request it may not be able to do: it says so plainly |
| 9 | near | "Hey emet, stop." | a one-word turn |
| 10 | near | A follow-up to its last answer, in your own words | a turn that needs what was said before |

From about 3 m it may take more than one try to wake. Count the tries in
the notes and keep going.

## What each box means

- **Asked:** it answered what you asked.
- **Whole:** it never cut you off. The words after `you:` on the screen are
  what you said: nothing missing at the start, and no word in front, such as
  "Hey." or "Met".
- **Held:** turn 4 only. It waited through the pause and answered the whole
  question.
- **Voice:** the voice sounds right: clear, at its usual speed, with no
  clicks, gaps or cut-off words.
- **Soon:** the answer came soon enough for a conversation.
- **Body:** turn 5 only. It said it cannot come, and why.

## The sheet

| # | Asked | Whole | Held | Voice | Soon | Body | Tries and notes |
|---|---|---|---|---|---|---|---|
| 1 | [ ] | [ ] | | [ ] | [ ] | | |
| 2 | [ ] | [ ] | | [ ] | [ ] | | |
| 3 | [ ] | [ ] | | [ ] | [ ] | | |
| 4 | [ ] | [ ] | [ ] | [ ] | [ ] | | |
| 5 | [ ] | [ ] | | [ ] | [ ] | [ ] | |
| 6 | [ ] | [ ] | | [ ] | [ ] | | |
| 7 | [ ] | [ ] | | [ ] | [ ] | | |
| 8 | [ ] | [ ] | | [ ] | [ ] | | |
| 9 | [ ] | [ ] | | [ ] | [ ] | | |
| 10 | [ ] | [ ] | | [ ] | [ ] | | |

Anything else you noticed, one line each:

1.
2.
3.

## When something goes wrong

- **It does not wake:** say the name again, a little closer. Note the tries.
- **It answers before you finish:** leave Whole unticked for that turn and
  go on.
- **The run stops with an error:** write down its last line, then run the
  same block again. The log keeps both.

## Afterwards

Tell the agent "sitting done", with the rows and boxes left unticked and a
word on each, or a photo of this sheet. The agent then collects the log and
checks it, from the laptop:

```sh
python tools/body/run.py --release X.Y.Z --collect-only --fetch '~/sitting-X.Y.Z.log'
```

That copies the log to `releases/vX.Y.Z/body/<date>/`, checks the line
under each wake against the footer's counts, and adds the sitting's runs to
the table of the date each flag last ran on the body. The sheet's ticks go
into the pull request beside the numbers.
