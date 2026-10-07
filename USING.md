# Using the assistant

This is a guide to the interface: what each panel is for, how a turn actually
goes, and what to do when the answer looks wrong. For installing and starting
it, see [README.md](README.md). For configuration details, [RUNNING.md](RUNNING.md).

## The one idea worth knowing first

The assistant reads your `.trn` and `.2h` files directly, and it is restricted
to what those files contain — which is what *you* can see. It never reads
`ftherlnd`, the host's full-information file. Enemy army sizes, rival
treasuries, what is hiding in an unscouted province: it does not know, and it
is built to say so rather than guess.

That restraint is the point. A plausible invented number is worse than no
number, because Dominions resolves the turn either way and you cannot tell
afterwards which one you acted on.

So when it says "I cannot see that", it is working correctly.

## Three workspaces

The dropdown at the top left switches between them. They are separate on
purpose — each gets its own tools, its own chats, and its own character
binding.

| Workspace | For | Tools |
|---|---|---|
| **Live turn** | Playing a turn of a game you have ingested | 54 |
| **Pretender design** | Building a god before the game starts | 16 |
| **Open chat** | Talking about the game with no save loaded | 15 |

**Live turn** needs an ingested game. Pick one from the game dropdown beside
the workspace selector.

**Pretender design** needs no save at all. Choose a nation and it exposes exact
chassis, path, scale and blessing arithmetic for that nation. Use this before
you have started a game.

**Open chat** has no access to any save. It is for comparing nations, asking
how a mechanic works, or thinking out loud before committing. It cannot see
your game, which is what makes it safe to use mid-game without leaking your own
position into a conversation you might share.

## A turn, start to finish

1. Finish your turn in Dominions as usual. The assistant watches your save
   folder while it runs, so the new `.trn` is picked up on its own — there is
   nothing to import.
2. Open the **Live turn** workspace and select the game.
3. Ask it what it sees. "What happened last turn?" or "Where am I exposed?" is
   enough to start. It will call read tools and show you what it found.
4. Talk through the turn. It can read provinces, commanders, armies, income,
   research, items, events and messages, and it will cite what it read.
5. When you agree on a plan, let it **record orders**. These are written to a
   database, not to your save file — nothing touches your game yet.
6. Review the orders in the **Orders** panel.
7. **Materialise** them when you are satisfied. *Now* it writes to your `.2h`.
8. Open Dominions and submit the turn as normal.

Steps 5 and 7 are deliberately separate. Recording an order is a note; writing
it to your save is an edit to a real game file.

## Writing to your save

Writes are off until you turn them on. The **allow writes** checkbox in the
header is the gate, and a warning appears while it is on.

With writes off, the assistant can read, plan, discuss and record intentions,
and can change nothing. This is a reasonable way to use it permanently if you
would rather issue the orders yourself in-game.

With writes on, `materialize_orders` edits your `.2h`, and `submit_turn` marks
it finished. In a network game the file still has to reach the host — your
Dominions client does that when you submit, exactly as it would normally.

## The side panels

### Orders
What the assistant has recorded for this turn, per commander, with its
reasoning. Check here before materialising. An order you disagree with is a
conversation, not a problem — tell it and it will re-record.

### Notes
A scratchpad that survives between turns. Add a plan, a threat, a question or a
commitment, optionally with a tag.

Two controls matter:

- **Pin** a note to keep it in front of the assistant permanently. Use it for
  durable commitments — a non-aggression pact, a long-term plan, a province you
  have promised not to take.
- **Resolve** a note once it no longer applies.

Open notes are injected into the assistant's context automatically. That is why
resolving matters: a stale note is not merely untidy, it is actively misleading
the assistant every turn until you clear it.

### Playbook
Standing guidance in your own words — how you like to play, what you never do,
conventions you want followed.

Matching is **literal and inspectable**, which is unusual and worth
understanding. A phrase matches that phrase; a single word matches that whole
word. There is no fuzzy interpretation, so you can predict exactly when a piece
of guidance will fire. Entries marked **always inject** are included every time
regardless of what you said.

Use the playbook for rules ("never leave the capital undefended"), and Notes
for facts about the current situation.

### Context
Shows exactly what would be assembled and sent to the model — playbook entries,
open notes, game state, everything — **without contacting the model** and
without recording a run.

If the assistant is behaving oddly, look here first. Most surprising answers
turn out to be surprising inputs: a stale note, a playbook entry firing when
you did not expect it, or a game state that is not the one you thought.

### History
Decisions the assistant has recorded, each with the reasoning it gave at the
time. Useful two turns later when you want to know why you committed to
something.

### Lessons
Conclusions it has drawn from how games have actually gone, each with the
evidence behind it. Read the evidence before trusting the conclusion.

### Gaps
What the assistant knows it cannot see, and what experiment would settle each
one. This is the honest ledger of the project's blind spots.

On a fresh install this list is empty — the findings live in the developer's
own database and are not shipped. An empty list means "nothing recorded here",
not "no blind spots".

### Activity
Every tool call in the current conversation, with its arguments and its raw
result. When an answer looks wrong, this shows whether the tool returned
something wrong or the model read something right and reasoned badly. They are
different problems and the fix is different.

### Character
Optional. Imports SillyTavern-style character cards, or creates one here, so
the assistant answers in a particular voice. Bind a card per workspace.

The **persona** is you: a name and description the character can refer to with
`{{user}}`. Creator notes on an imported card are shown for your information
and are not sent to the model.

Voice only. A character changes how it talks, never what it can see.

### Tools
Every tool available in the current workspace, with its arguments. Worth
skimming once to learn what the assistant can actually do — most people
underestimate it and ask for less than it can give.

### Model
Endpoint and model selection, plus generation profiles. See
[README.md](README.md) for setting the endpoint up.

Profiles hold the system prompt, temperature, token limits, request timeout and
tool mode. **Request timeout** is the one to reach for first: a slow local
model on a long context will exceed the default, and `0` disables the deadline
entirely.

## Two header toggles

**think aloud** shows the model's reasoning as it works, rather than only its
conclusion. Most useful when you disagree with an answer and want to see where
it went astray.

**stream** shows text as it is generated instead of waiting for the whole
reply. On a slow local model this is the difference between watching a blank
screen for two minutes and reading along.

## The verification page

`/verify` runs every read tool against your live save and shows the unmodified
output, **without contacting any model at all**.

Start here if you are new, and come back whenever you are unsure whether to
believe something. It separates two questions that are easy to confuse: can the
assistant *see* this correctly, and is it *reasoning* about it correctly. The
page answers the first on its own.

## Reference libraries

If you installed them, the assistant can also search:

- **The manual** — Illwinter's own documentation, cited by page number, so you
  can check any rule it quotes against your own copy.
- **The community wiki** — mechanics and nation write-ups.
- **Strategy videos** — transcripts of videos you have added, cited by
  timestamp with a link to the moment.

They are ranked deliberately, and the assistant is told the order: the live
game beats the manual, the manual beats the wiki, the wiki beats a video guide.
A video is one person's opinion, often about an older edition, transcribed
automatically — good for plans, poor for numbers.

Paste a YouTube link into the chat and ask for it to be indexed, and it will be
searchable from then on.

## When something looks wrong

**It says it cannot see something.** Usually correct. Check **Gaps**, and check
`/verify` to see what is genuinely available.

**It gives a strange answer.** Check **Context** for what it was actually sent,
then **Activity** for what the tools actually returned. One of those two is
nearly always the cause.

**It keeps acting on something that is no longer true.** An open note. Resolve
it in **Notes**.

**It ignores your instruction.** Playbook matching is literal — check the entry
fires on the words you are actually using, or mark it *always inject*.

**It is cut off mid-reply.** Raise **max tokens** in the Model panel.

**It times out.** Raise **request timeout**, or set it to `0`.

**A turn did not appear.** Some saves are refused on purpose when a field
cannot be decoded unambiguously. The refusal is logged in the terminal where
you started it. The assistant would rather skip a turn than report a number it
cannot prove.
