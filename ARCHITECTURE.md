# Architecture & Design Decisions

This document captures key architectural decisions and their rationale so context
can be restored quickly after a reset. It should be kept up to date as decisions
are made or revised.

---

## Core Goal

A helper program that runs alongside Dominions 6 and enables an AI assistant to:

1. **Know the full game state** from the very start of turn 1
2. **Work for any player** — host or non-host, singleplayer or multiplayer
3. **See live intra-turn state** as the player issues orders (treasury/gem changes)
4. **Make or suggest decisions** based only on player-visible information (no cheating)

---

## Data Source Hierarchy

Three data sources are layered by reliability and availability. **All three are
wanted long-term; priority order is 1 → 2 → 3.**

### 1. `.trn` file (PRIMARY — highest priority)

**What it is:** The turn file the game server sends to each player at the start of
their turn. Dom6's client reads this file to render the entire game UI — it is
the authoritative source for all player-visible game state.

**Why it is primary:**
- Available to **every player** regardless of who hosts or where the server runs
- Contains **everything the player can see**: treasury, gems, all owned/visible
  provinces, units, commanders, army compositions, diplomacy, magic sites, events
- **Works from turn 1** — the first `.trn` is delivered before any orders are issued
- Deterministic binary format (same across sessions/machines)
- No process privileges required
- The AI should reason only from player-visible information anyway — using `.trn`
  as the primary source enforces this cleanly

**Location:** `<game_dir>/savedgames/<game_name>/<nation_name>.trn`
(or equivalent path depending on OS / Steam install)

**Format:** Binary, community-reverse-engineered. Dom5 format is nearly identical;
[dom5inspector](https://github.com/larzm42/dom5inspector) is the best reference.
Records appear to be length-prefixed (4-byte LE length header followed by payload).

**Implementation target:** `src/dom6_assistant/file_reader/formats/trn.py`

---

### 2. Live process memory (SECONDARY — real-time intra-turn deltas)

**What it is:** Direct reads from the running `dom6_amd64` process via
`/proc/<pid>/mem` on Linux.

**Why it is secondary:**
- The `.trn` only updates between turns. During the player's turn (queuing units,
  casting rituals, moving armies), treasury and gems change in real time.
  Memory reading captures this live state for the UI overlay and AI feedback.
- Also useful for values not easily surfaced in `.trn` (e.g. exact gold remaining
  after every recruitment decision).

**Critical design decision — pointer chain, NOT scan/narrow:**

The naive approach (scan for gold value, narrow across turns) has a fatal flaw:
memory addresses change every session due to ASLR. The scan produces no reusable
knowledge. A multi-turn bootstrap is unacceptable for a v1.0 tool.

**The correct approach is a pointer chain anchored to the static binary segment:**

```
dom6_amd64 binary load base  (read from /proc/<pid>/maps — instant, always correct)
  + fixed_offset_A            (static segment, survives ASLR)
  → pointer in heap/data
  + fixed_offset_B
  → nation struct base
  + known_field_offset        (e.g. +0x000 = gold, +0x00C = nation_id, +0xF04 = gems)
  → live field value
```

The pointer chain offsets are **constants in the binary** — they survive ASLR
because they describe relative structure, not absolute addresses. They require
a one-time reverse engineering effort (pointer scan on a known `gold_address`),
after which the tool finds every address in < 1 ms on every session start.

**Known field offsets (relative to gold field address, confirmed across 5+ turns):**

| Offset | Type  | Field        | Confidence |
|--------|-------|--------------|------------|
| +0x000 | int32 | gold         | confirmed  |
| +0x00C | int16 | nation_id    | confirmed  |
| +0xF04 | int32 | gem: fire         | confirmed — debug: 97  |
| +0xF08 | int32 | gem: air          | confirmed — debug: 91  |
| +0xF0C | int32 | gem: water        | confirmed — debug: 79  |
| +0xF10 | int32 | gem: earth        | confirmed — debug: 105 |
| +0xF14 | int32 | gem: astral       | confirmed — debug: 84  |
| +0xF18 | int32 | gem: death        | confirmed — debug: 101 |
| +0xF1C | int32 | gem: nature       | confirmed — debug: 89  |
| +0xF20 | int32 | gem: glamour      | confirmed — debug: 87 (Dom6 new gem type) |
| +0xF24 | int32 | gem: blood_slaves | confirmed — debug: 99 (9th slot) |

**Gem type ordering:** Dom6 memory order matches Dom5 display order for the first 7 slots,
then adds Glamour (Dom6 new) and Blood_slaves.
Memory order: Fire(0), Air(1), Water(2), Earth(3), Astral(4), Death(5), Nature(6), Glamour(7), Blood_slaves(8)
.trn order:   Fire(0), Air(1), Water(2), Earth(3), Astral(4), Death(5), Nature(6), Glamour(7), Blood(8) — 9 slots
The journal correlation engine cross-references journal entries with memory
snapshots to resolve this — see `src/dom6_assistant/web/routes/correlate.py`.

**Note:** The `.trn` file uses Dom6 display order (Fire=0, Air=1, Water=2, Earth=3, Astral=4,
Death=5, Nature=6, Glamour=7, Blood=8) — 9 slots, fully confirmed. Glamour confirmed via Early
Vanheim t1 with glamour=1 at .trn offset 0x1515e.

**TODO:** Run a pointer scan against the static segment of `dom6_amd64` to find
the chain from a fixed offset to the nation struct. This makes live memory
detection instant and session-independent. The existing scan/narrow CLI and
correlation UI are interim scaffolding only.

**Why NOT DLL/LD_PRELOAD injection:**
- Dom6 runs inside Steam's pressure-vessel container; LD_PRELOAD may not propagate
- ptrace-based injection is fragile across kernel versions
- Provides no meaningful advantage over pointer-chain reading for data access
- Significantly more complexity for marginal gain

**Implementation:** `src/dom6_assistant/memory_reader/_linux.py`
Process discovery: `dom6_amd64` preferred over `dom6.sh` (the latter is a bash wrapper).

---

### 3. `ftherlnd` save file (BONUS — server-side full state)

**What it is:** The server-side save file written when a multiplayer game is
hosted locally (singleplayer always has it). Contains the *full* world state
including AI nation data, hidden information, and server-side calculations.

**Why it is tertiary / bonus:**
- **Only available when self-hosting** — fails requirement B (non-host players
  have no access to this file)
- Contains information beyond what the player should legitimately see (AI
  opponents' army compositions, hidden province ownership, etc.) — using it
  for AI decisions would constitute cheating in a competitive context
- Genuinely useful for singleplayer or analysis mode where full-information
  play is acceptable

**File name:** `ftherlnd` (not `.fatherland`) in the game's savedgames directory.
**Format:** Binary, length-prefixed records. Magic variant `DOMg` observed.
A 500-byte block was identified (preceded by `f4 01 00 00` = 500 LE).

**Implementation target:** `src/dom6_assistant/file_reader/formats/ftherlnd.py`

---

## Data Flow (Target Architecture)

```
New turn arrives
│
├─ .trn file detected (filesystem watch)
│   └─ Parse → GameState snapshot → persist to journal DB
│       └─ Notify AI layer → strategic analysis → recommendations
│
├─ During player's turn
│   └─ Memory pointer chain → live treasury / gem values
│       └─ UI overlay: "350 gold remaining after queued orders"
│
└─ (Optional, singleplayer/self-host only)
    └─ ftherlnd parse → full world state → bonus AI context
```

---

## What the AI Should and Should Not Use

| Data source    | AI decisions | Why |
|----------------|--------------|-----|
| `.trn` content | YES          | Player-visible; fair game |
| Live memory (treasury/gems during turn) | YES | Player sees this in the UI |
| `ftherlnd` AI nation data | NO (by default) | Hidden information; cheating in MP |
| `ftherlnd` in singleplayer analysis mode | Opt-in | User's choice |

---

## Current Implementation Status

**This file describes design intent. For what actually works today, read
[STATUS.md](STATUS.md), which is kept current; this section is a summary of
where each layer stands and is the part most likely to drift.**

| Layer | Status |
|-------|--------|
| `.trn` / `.2h` / `.map` parsers | Working. Anchored signature search, never absolute offsets |
| `ftherlnd` parser | Works, and is **barred from the gameplay path** — build-time decode oracle only |
| Visibility boundary (`agent/visibility.py`) | The only route to game state; every tool goes through it |
| Reference data (`reference.sqlite3`) | 50 tables; unit costs and province economics ported from the 6.36 binary |
| Order writing (`orders/materialize.py`) | Confirmed client and host. Recruitment, research, rituals, forging, empowerment, province defence, mercenary bids, battle setup, squads |
| Tool surface | 60 tools, read and write, all exercised by tests |
| Memory reader (Linux, scan/narrow) | Working — and now largely unnecessary |
| Memory reader (pointer chain) | Not built, and no longer the plan: see below |
| AI integration | Working against an OpenAI-compatible endpoint |
| Web UI | Two apps; `ui.app` is the one to run |

### Where the plan changed

**The memory layer turned out to be the wrong bet.** This document originally
treated live process memory as the second data source, with a pointer chain as
the eventual fix for ASLR. In practice almost everything came from the files
instead, and the binary was used differently than expected: not to read live
state, but to **transcribe calculators** — province income, resources,
supplies, recruitment points and unit costs are ports of 6.36 functions, run
against file-derived inputs. That is reproducible offline and survives a
restart, which a pointer chain never would have.

The scan/narrow CLI remains useful for one-off investigation. It is not on the
path to anything.

**Some things the binary was expected to hold are not in it.** Item forge costs
were assumed to need extraction and turned out to depend only on the path level,
readable off the game's own screens in two minutes. The general lesson, recorded
because it cost real effort: check what a screen says before deciding a value
must be decoded.
