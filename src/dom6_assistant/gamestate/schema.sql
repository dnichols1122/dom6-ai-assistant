-- game.sqlite3 — per-turn snapshots of everything parsed out of the .trn.
--
-- Separate from reference.sqlite3 on purpose: reference data is regenerated
-- wholesale on a game patch, while this accumulates history and must never be
-- rebuilt. Keeping them apart means a patch can never destroy game history.
--
-- Everything is stored per (game, turn) rather than overwritten, so
-- turn-over-turn diffs ("what did I lose last turn?") are a query rather than
-- a feature that has to be designed in.

CREATE TABLE IF NOT EXISTS games (
    id           INTEGER PRIMARY KEY,
    -- One row is one player-visible view. Single-human saves retain the raw
    -- game name; multi-human saves use game::nation_slug so their private
    -- .trn histories and order intent can never overwrite one another.
    name         TEXT    NOT NULL UNIQUE,
    save_name    TEXT,                      -- raw .trn game/save-directory name
    nation_id    INTEGER,                   -- the player's nation
    nation_slug  TEXT,                      -- e.g. mid_marignon, from the filename
    save_dir     TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS turns (
    id            INTEGER PRIMARY KEY,
    game_id       INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    turn          INTEGER NOT NULL,
    ingested_at   TEXT NOT NULL DEFAULT (datetime('now')),
    trn_sha256    TEXT,                     -- so a re-ingest of identical bytes is a no-op
    trn_bytes     INTEGER,
    UNIQUE(game_id, turn)
);

-- Nation-level state: treasury, gems, and anything else that is one row.
CREATE TABLE IF NOT EXISTS nation_state (
    turn_id   INTEGER PRIMARY KEY REFERENCES turns(id) ON DELETE CASCADE,
    gems_fire INTEGER, gems_air INTEGER, gems_water INTEGER, gems_earth INTEGER,
    gems_astral INTEGER, gems_death INTEGER, gems_nature INTEGER,
    gems_glamour INTEGER, gems_blood INTEGER,
    gold        INTEGER                      -- treasury; 600 for everyone on turn 1
);

-- Every nation in the game, from the per-nation records. The roster is not
-- hidden information: it is the player list, which the game shows in its own
-- score tab. Gold is each nation's treasury.
CREATE TABLE IF NOT EXISTS nation_roster (
    turn_id   INTEGER NOT NULL REFERENCES turns(id) ON DELETE CASCADE,
    nation_id INTEGER NOT NULL,
    gold      INTEGER,
    PRIMARY KEY (turn_id, nation_id)
);

CREATE TABLE IF NOT EXISTS provinces (
    turn_id           INTEGER NOT NULL REFERENCES turns(id) ON DELETE CASCADE,
    province_id       INTEGER NOT NULL,
    name              TEXT,
    name2             TEXT,
    owner_nation_id   INTEGER,
    is_capital        INTEGER NOT NULL DEFAULT 0,
    population        INTEGER,
    unrest            INTEGER,
    land_gold         INTEGER,        -- body+30; saved `landgold` event delta
    administrative_owner INTEGER,     -- body+36; fort administrative controller
    province_defense  INTEGER,
    dominion_owner    INTEGER,
    dominion_strength INTEGER,
    -- body+82. This is what the GAME displays, and it can differ from the .map
    -- file's terrain: province 98 reads Waste in game while the map says Plains.
    -- The map's original terrain is at body+90 and is NOT what to show a player.
    terrain_flags     INTEGER,        -- bitmask; join ref.map_terrain_types
    -- Recruitment queue, body+26 and +28. Non-zero only in fort provinces,
    -- which is how they were identified: across ftherlnd's full-information
    -- view of all 166 provinces they are non-zero in exactly the six capitals.
    commanders_queued INTEGER,
    troops_queued     INTEGER,
    wall_integrity    INTEGER,        -- body+72; the Citadel screen's 1500/1500
    -- raw_b32 is the raw resource-calculator input, NOT the displayed total:
    -- 0x46caf0 applies scales/unrest/sites and 0x46d110 redistributes through
    -- forts. raw_b51 remains unidentified and is NOT displayed supplies.
    -- Panel totals are computed by the client and do not appear in the file.
    raw_b32           INTEGER,
    raw_b51           INTEGER,
    fort_type         INTEGER,        -- 0 = none; value differs by nation
    has_laboratory    INTEGER,        -- body+41
    has_temple        INTEGER,        -- body+42
    order_scale       INTEGER, productivity_scale INTEGER, heat_scale INTEGER,
    growth_scale      INTEGER, luck_scale INTEGER, magic_scale INTEGER,
    PRIMARY KEY (turn_id, province_id)
);

CREATE INDEX IF NOT EXISTS idx_prov_owner ON provinces(turn_id, owner_nation_id);

-- Magic sites per province -> reference.sqlite3 magic_sites. Populated only
-- where the player can see them: their own provinces, and thrones, which the
-- game marks on the map for everyone. Sites drive gem income, and also the
-- income/resources/recruitment figures the .trn does not store, so this is the
-- table those calculations will be built on.
CREATE TABLE IF NOT EXISTS province_sites (
    turn_id     INTEGER NOT NULL REFERENCES turns(id) ON DELETE CASCADE,
    province_id INTEGER NOT NULL,
    slot        INTEGER NOT NULL,
    site_id     INTEGER NOT NULL,
    PRIMARY KEY (turn_id, province_id, slot)
);

-- The province graph. Verified against the game's own .map file for all 99
-- surface provinces; the .trn additionally carries underworld links, which the
-- surface map file does not list.
CREATE TABLE IF NOT EXISTS province_links (
    turn_id      INTEGER NOT NULL REFERENCES turns(id) ON DELETE CASCADE,
    province_id  INTEGER NOT NULL,
    neighbour_id INTEGER NOT NULL,
    PRIMARY KEY (turn_id, province_id, neighbour_id)
);

-- Static per-map facts read from the .map file rather than decoded: border
-- type between two provinces (mountain pass, river, wall) determines whether
-- ground troops can use a link at all, which flyers ignore.
CREATE TABLE IF NOT EXISTS map_borders (
    game_id      INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    province_id  INTEGER NOT NULL,
    neighbour_id INTEGER NOT NULL,
    border_flags INTEGER NOT NULL,
    PRIMARY KEY (game_id, province_id, neighbour_id)
);

-- One row per soldier and per commander: they share the same 173-byte record.
--
-- IDENTITY: a unit is identified by (turn_id, file_offset) — its position in
-- that turn's file. There is deliberately no cross-turn unit identity, because
-- the file does not provide one.
--
-- `instance_id` looks like the obvious key and is NOT usable as one. **The game
-- recycles instance ids.** Between turns 6 and 7 of the fixture, instance 2530
-- went from a Crossbowman with 10 hp, age 22 and 9 experience to a Destrier with
-- 22 hp, age 19 and 1 experience — a different unit wearing a dead one's id.
-- Joining two turns on instance_id silently merges unrelated units, and the
-- resulting row looks plausible, which is what makes it dangerous. Commander ids
-- are recycled the same way: id 182 was Estorgant, then Hector Stark.
--
-- So `instance_id` is stored as an ordinary column, unique only within a turn.
-- Anything asking "what happened to this unit" must match on observable
-- properties (type, home province, age) and accept that the match is a
-- heuristic, or work from turn-level aggregates instead.
CREATE TABLE IF NOT EXISTS units (
    turn_id       INTEGER NOT NULL REFERENCES turns(id) ON DELETE CASCADE,
    file_offset   INTEGER NOT NULL,          -- identity within this turn
    instance_id   INTEGER NOT NULL,          -- NOT stable across turns; see above
    type_id       INTEGER NOT NULL,          -- -> reference.sqlite3 units.id
    nation_id     INTEGER,
    hp            INTEGER,
    age           INTEGER,
    experience    INTEGER,                   -- +8; +1 per turn alive, +4 more if it fought
    kills         INTEGER,                   -- +38
    afflictions   INTEGER,                   -- -28, bitmask; join ref.afflictions
    squad_id      INTEGER,                   -- +34; 65535 = follows no commander
    province_id   INTEGER,                   -- +4, where it is now
    home_province INTEGER,                   -- +6, where it was recruited
    is_mount      INTEGER,                   -- +51 == 0
    -- RETRACTED: has_mount at +167 was the NEXT record's mount marker, not a
    -- rider flag. A unit's own fields span type-28 .. type+144; +145 onward
    -- belongs to the following record. See units.py.
    has_fought    INTEGER,                   -- -15 bit 0x20; killed or was wounded
    is_pretender  INTEGER,                   -- -11 bit 0x04
    PRIMARY KEY (turn_id, file_offset)
);

CREATE INDEX IF NOT EXISTS idx_units_type ON units(turn_id, type_id);
CREATE INDEX IF NOT EXISTS idx_units_nation ON units(turn_id, nation_id);
-- Within a turn only. Never join two turns on this column.
CREATE INDEX IF NOT EXISTS idx_units_instance ON units(turn_id, instance_id);

-- Named commanders, including other nations' pretenders, which are visible
-- from turn 1 and are legitimate player-visible information.
--
-- Names come from the .trn's name table and stats from the 173-byte records,
-- and **the two cannot be joined**. Nothing inside a stat record holds its
-- commander id — every u16 and u32 offset from -40 to the record end was checked
-- against five pretenders whose ids the nation records give, and none matches.
-- A positional join was tried and refuted: the orderings agree for pretenders
-- (created at game start in nation order, so sorted by construction) but break
-- on our own roster, where Sugaar is second by commander id and fifth by
-- instance id.
--
-- So this table carries names without stats. `commander_id` is populated from
-- the .2h, which lists ids and names together for our own nation — the one place
-- the link exists. Rival commanders have a name and no id, which is correct:
-- a player cannot see their stats either.
CREATE TABLE IF NOT EXISTS commanders (
    turn_id      INTEGER NOT NULL REFERENCES turns(id) ON DELETE CASCADE,
    file_offset  INTEGER NOT NULL,           -- identity within this turn
    name         TEXT NOT NULL,
    commander_id INTEGER,                    -- from the .2h; NULL for rivals
    nation_id    INTEGER,
    type_id      INTEGER,
    PRIMARY KEY (turn_id, file_offset)
);

-- Unit gold costs observed in the game's own files.
--
-- The scraped reference data does NOT contain unit costs: BaseU.csv carries
-- only `basecost`, the game's raw modifier (10020 for both Paladin and Knight
-- of the Chalice, which really cost 215 and 70), and dom6inspector computes the
-- displayed price from stats. So costs are learned rather than looked up —
-- every .2h with a recruitment queue in it teaches us a few more, and they are
-- exact because they are what the game charged.
CREATE TABLE IF NOT EXISTS observed_unit_costs (
    unit_type_id INTEGER PRIMARY KEY,
    gold         INTEGER NOT NULL,
    source       TEXT,                  -- e.g. '2h:example_game:t1'
    observed_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Free-form notes the assistant keeps across turns: plans, diplomatic reads,
-- "watch this border". Nothing here comes from the game — it is the equivalent
-- of the notes a human player keeps in their head.
CREATE TABLE IF NOT EXISTS scratchpad (
    id         INTEGER PRIMARY KEY,
    game_id    INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    turn       INTEGER,                     -- turn it was written on
    tag        TEXT,                        -- 'plan', 'threat', 'diplomacy', ...
    note       TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'open', -- open | resolved
    pinned     INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_scratch_game ON scratchpad(game_id, turn);

-- Durable, player-visible evidence that a hidden foreign province protection
-- intercepted one of our spells. The game report names the province but not
-- the ward, and reveals neither its duration nor whether it remains active on
-- later turns. Candidate identities are explicitly inference-only.
CREATE TABLE IF NOT EXISTS province_protection_observation (
    id                       INTEGER PRIMARY KEY,
    game_id                  INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    observed_turn            INTEGER NOT NULL,
    message_id               INTEGER NOT NULL,
    province_id              INTEGER NOT NULL,
    province_name            TEXT,
    attacking_spell          TEXT,
    confirmed_effect         TEXT NOT NULL,
    retaliation_reported     TEXT,
    candidate_wards_json     TEXT NOT NULL DEFAULT '[]',
    evidence                 TEXT NOT NULL,
    created_at               TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(game_id, observed_turn, message_id)
);

CREATE INDEX IF NOT EXISTS idx_province_protection_observation
    ON province_protection_observation(game_id, province_id, observed_turn);

-- Player-visible bookkeeping for Call God.  Dominions does not reveal the
-- accumulated random total; a human can only remember which priests prayed
-- on each resolved turn and maintain the resulting lower/upper bounds.  We do
-- the same.  A row is the latest observed planning state for one turn, so it
-- is deliberately replaced when the player changes orders before hosting.
CREATE TABLE IF NOT EXISTS call_god_observation (
    game_id          INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    turn             INTEGER NOT NULL,
    pretender_id     INTEGER,
    pretender_dead   INTEGER NOT NULL,
    minimum_points   INTEGER NOT NULL DEFAULT 0,
    maximum_points   INTEGER NOT NULL DEFAULT 0,
    active_priests   TEXT NOT NULL DEFAULT '[]',
    observed_at      TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (game_id, turn)
);

CREATE INDEX IF NOT EXISTS idx_call_god_game_turn
    ON call_god_observation(game_id, turn);

-- ---------------------------------------------------------------------------
-- INTENT: what the assistant has decided to do.
--
-- The .2h is **derived state**, never an accumulator. Every materialisation
-- starts from the pristine .2h the game wrote and applies the complete current
-- intent, so the file is a pure function of these rows.
--
-- That matters because an assistant changes its mind. Patching a .2h in place
-- twice - move to 98, then defend - leaves the destination from the first edit
-- sitting behind the order code from the second, and nothing detects it. The
-- turn then resolves as something nobody intended. Rebuilding from a pristine
-- base makes that structurally impossible.
--
-- Rows are HISTORY: nothing is updated or deleted, and a change is a new row.
-- The current order for a commander is the highest id for that (game, turn,
-- commander). Keeping history costs nothing and makes "what did I order on turn
-- 6, and why" a query - which matters for a project built on turn diffs.

CREATE TABLE IF NOT EXISTS order_intent (
    id             INTEGER PRIMARY KEY,
    game_id        INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    turn           INTEGER NOT NULL,
    commander_id   INTEGER NOT NULL,   -- from the .2h; ours only
    commander_name TEXT,               -- denormalised: ids are recycled on death
    order_name     TEXT NOT NULL,      -- a verified name from ORDER_CODES
    -- Historical column name retained for existing databases. This is the raw
    -- +116 order parameter: province id, item id, magic-path index, building
    -- index, or 0 depending on order_name.
    destination    INTEGER,
    -- Why this order was chosen. Written at decision time and read back later,
    -- which is the point: an assistant that has lost the context in which it
    -- decided something can recover the reasoning without re-deriving it, and it
    -- only enters context when actually queried.
    rationale      TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_intent_turn
    ON order_intent(game_id, turn, commander_id, id);

-- The order that will actually be written: latest row wins per commander.
CREATE VIEW IF NOT EXISTS current_orders AS
SELECT i.*
FROM order_intent i
JOIN (SELECT game_id, turn, commander_id, MAX(id) AS id
      FROM order_intent GROUP BY game_id, turn, commander_id) latest
  ON latest.id = i.id;

-- Extra fields belonging only to code-9/code-60 ritual orders.  The parent
-- order row keeps rituals in the same latest-row-wins history as movement,
-- research, forging, and every other commander decision; splitting these
-- fields into a child avoids adding spell-specific nullable columns to every
-- ordinary order.
CREATE TABLE IF NOT EXISTS ritual_intent (
    order_intent_id INTEGER PRIMARY KEY
        REFERENCES order_intent(id) ON DELETE CASCADE,
    spell_id        INTEGER NOT NULL,
    spell_name      TEXT,
    gem_path        INTEGER NOT NULL, -- FAWESDNGB index; Holy is not valid
    gem_cost        INTEGER NOT NULL,
    target_province INTEGER,          -- NULL writes the observed zero sentinel
    -- Stable .2h commander id selected by the tool.  At materialisation time
    -- this is resolved to the runtime handle stored in that commander's block,
    -- which is the value the client writes at ritual offset +128.
    target_commander_id INTEGER,
    -- Stable ordinary-unit instance selected by unit-targeted rituals such as
    -- Gift of Reason and Divine Name. Materialisation resolves it to the
    -- volatile runtime handle and writes that handle to both +124 and +128.
    target_unit_instance_id INTEGER,
    -- Unequipped treasury item carried by an item-transport ritual. The item
    -- id is written at +132 and its treasury slot is zeroed atomically.
    target_item_id  INTEGER,
    -- Magic item selected through Wish's free-text prompt. The client resolves
    -- the name immediately and stores result-family 10001 at +124 plus this
    -- stable item id at +128. Unlike target_item_id, this is created by the
    -- spell and must never be removed from the treasury during materialisation.
    wish_item_id    INTEGER,
    -- Named unit type selected through Wish. Horrors use the same typed unit
    -- selector but serialize result family 10018 instead of ordinary-unit
    -- family 10002. NULL on 10018 means the host chooses a random Horror.
    wish_unit_id    INTEGER,
    -- Nation selected by Wish's kill-pretender family.
    wish_nation_id  INTEGER,
    -- Typed non-item Wish result. The original UI prose is not serialized;
    -- controlled result names map to the client codes written at +124.
    wish_result     TEXT,
    -- Effect id of the global enchantment a Dispel removes. Stored by
    -- identity rather than by chain slot: slots are stable but a vacated
    -- one is reused by the next global cast, so the slot is resolved
    -- afresh at materialisation.
    target_global_effect_id INTEGER,
    monthly         INTEGER NOT NULL DEFAULT 0
);

-- Forging, which shares the ritual's fields and gem reservation. The primary
-- path cost is at +120 and the optional secondary path cost is at +124.
CREATE TABLE IF NOT EXISTS forge_intent (
    order_intent_id INTEGER PRIMARY KEY
        REFERENCES order_intent(id) ON DELETE CASCADE,
    item_id         INTEGER NOT NULL,
    item_name       TEXT,
    gem_path        INTEGER NOT NULL, -- FAWESDNGB index
    gem_cost        INTEGER NOT NULL, -- what is RESERVED: post-rebate
    secondary_gem_path INTEGER,       -- null for a single-path item
    secondary_gem_cost INTEGER NOT NULL DEFAULT 0
);

-- Empowerment. The .2h does NOT record the cost — every observed empowerment
-- leaves +120 at zero while the national pool still pays — so the reservation
-- is computed and stored here, and a replacement recomputes rather than
-- reading the old figure back out of the file.
CREATE TABLE IF NOT EXISTS empowerment_intent (
    order_intent_id INTEGER PRIMARY KEY
        REFERENCES order_intent(id) ON DELETE CASCADE,
    gem_path        INTEGER NOT NULL, -- FAWESDNGB index; Holy is not empowerable
    target_level    INTEGER NOT NULL,
    gem_cost        INTEGER NOT NULL
);

-- Recruitment intent, same rules.
CREATE TABLE IF NOT EXISTS recruit_intent (
    id           INTEGER PRIMARY KEY,
    game_id      INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    turn         INTEGER NOT NULL,
    province_id  INTEGER NOT NULL,
    unit_type_id INTEGER NOT NULL,
    quantity     INTEGER NOT NULL DEFAULT 1,
    rationale    TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Lessons that outlive a single game: rules of the game the assistant worked
-- out and wants to keep. Distinct from `scratchpad`, which is per-playthrough
-- and dies with it. Nothing here comes from a save file - it is what a returning
-- player would remember, and it is what stops the same mistake being rediscovered
-- every game.
CREATE TABLE IF NOT EXISTS lessons (
    id         INTEGER PRIMARY KEY,
    topic      TEXT NOT NULL,          -- 'bless', 'siege', 'ma_marignon', ...
    lesson     TEXT NOT NULL,
    -- Where this came from: 'experiment', 'wiki', 'game-screen', 'player'.
    -- Same discipline as the decode - a lesson with no evidence is a guess, and
    -- guesses that look like knowledge are what this project keeps having to
    -- retract.
    evidence   TEXT,
    learned_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_lessons_topic ON lessons(topic);

-- Player-authored strategic guidance.  This is deliberately distinct from
-- lessons written by the assistant: a rule supplied by the player must retain
-- its provenance and must never be silently edited into something the model
-- merely inferred.  `triggers_json` is an explicit array of words or phrases;
-- matching is deterministic and every injection is logged below.
CREATE TABLE IF NOT EXISTS playbook_entry (
    id            INTEGER PRIMARY KEY,
    game_id       INTEGER REFERENCES games(id) ON DELETE CASCADE,
    nation_id     INTEGER,
    title         TEXT NOT NULL,
    guidance      TEXT NOT NULL,
    tags_json     TEXT NOT NULL DEFAULT '[]',
    triggers_json TEXT NOT NULL DEFAULT '[]',
    priority      INTEGER NOT NULL DEFAULT 50,
    always_include INTEGER NOT NULL DEFAULT 0,
    enabled       INTEGER NOT NULL DEFAULT 1,
    created_by    TEXT NOT NULL DEFAULT 'player',
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_playbook_scope
    ON playbook_entry(enabled, game_id, nation_id, priority);

-- One row per actual model invocation. Besides making prompt composition
-- inspectable, the emitted, already-player-visible reasoning/prose/tool names
-- become deterministic retrieval signals for the *next* invocation. Decision
-- memory does not depend on this text; action rationales remain authoritative.
CREATE TABLE IF NOT EXISTS agent_context_run (
    id                  INTEGER PRIMARY KEY,
    game_id             INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    turn                INTEGER NOT NULL,
    user_message        TEXT NOT NULL,
    retrieval_text      TEXT NOT NULL DEFAULT '',
    injected_context    TEXT NOT NULL DEFAULT '',
    injected_entry_ids  TEXT NOT NULL DEFAULT '[]',
    visible_response    TEXT,
    emitted_signals     TEXT,
    tool_names_json     TEXT NOT NULL DEFAULT '[]',
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at        TEXT
);

CREATE INDEX IF NOT EXISTS idx_agent_context_run
    ON agent_context_run(game_id, turn, id);

CREATE TABLE IF NOT EXISTS context_injection_log (
    id                INTEGER PRIMARY KEY,
    context_run_id    INTEGER NOT NULL
        REFERENCES agent_context_run(id) ON DELETE CASCADE,
    playbook_entry_id INTEGER NOT NULL
        REFERENCES playbook_entry(id) ON DELETE CASCADE,
    score             INTEGER NOT NULL,
    matched_triggers  TEXT NOT NULL DEFAULT '[]',
    UNIQUE(context_run_id, playbook_entry_id)
);

-- A model stopping is not proof that a turn is ready. The final tool call
-- records an explicit handoff only after every commander has a reasoned
-- strategic order and the live .2h matches the assistant's last materialized
-- output. File and decision fingerprints make the handoff stale immediately
-- if either side changes later in the same turn.
CREATE TABLE IF NOT EXISTS agent_turn_completion (
    id                   INTEGER PRIMARY KEY,
    game_id              INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    turn                 INTEGER NOT NULL,
    summary              TEXT NOT NULL,
    outstanding_risks    TEXT NOT NULL,
    h2_sha256            TEXT NOT NULL,
    decision_fingerprint TEXT NOT NULL,
    completed_at         TEXT NOT NULL DEFAULT (datetime('now')),
    submitted_at         TEXT,
    UNIQUE(game_id, turn)
);

-- Supervisor attempts are durable operational evidence: which invocation ran,
-- why it stopped, and whether it produced a valid completion handshake.
CREATE TABLE IF NOT EXISTS autonomous_turn_run (
    id             INTEGER PRIMARY KEY,
    game_id        INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    turn           INTEGER NOT NULL,
    attempt        INTEGER NOT NULL,
    status         TEXT NOT NULL, -- running | incomplete | completed | error
    prompt         TEXT NOT NULL,
    final_text     TEXT,
    error          TEXT,
    started_at     TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at    TEXT,
    UNIQUE(game_id, turn, attempt)
);

CREATE INDEX IF NOT EXISTS idx_autonomous_turn_run
    ON autonomous_turn_run(game_id, turn, attempt);

-- ---------------------------------------------------------------------------
-- CAPABILITY REGISTRY: what a unit can actually be ordered to do.
--
-- Modelled as traits rather than as a flat per-unit-type table, because the
-- player's taxonomy is composable: scout, assassin, seductor, sacred, caster,
-- priest, and plain combatant, in any mix. A unit that is both a seductor and a
-- scout gets the union of both, and every priest is sacred while the reverse is
-- not true. A per-type enumeration would have to be re-harvested for every
-- nation and would still miss the combinations.
--
-- Availability is also CONTEXTUAL. Incite Rebellion only appears when the
-- commander stands in a province nobody owns or an enemy holds, so an order
-- absent from a menu proves nothing unless the context is recorded with it.
-- That is why province_context is part of the key rather than a note.
--
-- Everything here cites its evidence, same discipline as the decode: an
-- unverified capability must refuse rather than pass, because a tool that
-- silently permits an impossible order produces a turn that quietly does
-- nothing.

CREATE TABLE IF NOT EXISTS recruitable (
    nation_id        INTEGER NOT NULL,
    name             TEXT    NOT NULL,
    unit_type_id     INTEGER,            -- -> reference.sqlite3 units.id, when resolved
    gold             INTEGER,
    resources        INTEGER,
    commander_points INTEGER,            -- 4 means two turns at 3 points per turn
    is_commander     INTEGER NOT NULL DEFAULT 1,
    evidence         TEXT,               -- 'recruit-screen-tooltip', ...
    observed_at      TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (nation_id, name)
);

CREATE TABLE IF NOT EXISTS unit_traits (
    unit_name   TEXT NOT NULL,
    trait       TEXT NOT NULL,     -- scout | assassin | seductor | sacred | caster | priest | combatant
    evidence    TEXT,
    observed_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (unit_name, trait)
);

-- The harvest itself: which orders a unit's menu offers, and where.
CREATE TABLE IF NOT EXISTS order_capabilities (
    unit_name        TEXT NOT NULL,
    order_label      TEXT NOT NULL,     -- as the game writes it, e.g. 'Incite Rebellion'
    order_name       TEXT,              -- our ORDER_CODES key, once the code is known
    province_context TEXT NOT NULL,     -- 'owned' | 'foreign' | 'any'
    available        INTEGER NOT NULL,  -- 1 seen in the menu, 0 confirmed absent
    evidence         TEXT,
    observed_at      TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (unit_name, order_label, province_context)
);

-- What could not be reached, and why. A recorded gap is a result: it stops the
-- same dead end being re-explored, and it keeps "not harvested" distinct from
-- "harvested and absent", which the availability flag alone cannot express.
CREATE TABLE IF NOT EXISTS capability_gaps (
    subject   TEXT NOT NULL,     -- unit name, context, or nation
    reason    TEXT NOT NULL,
    noted_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (subject, reason)
);

-- Battle orders, from the "Set battle orders for <commander>" dialog reached by
-- clicking a squad's <set battle orders> on the Army Setup (T) screen.
--
-- The dialog has three parts: five Single Round Order rows, a Main Order, and a
-- Target.
--
-- NOTE, and this was got wrong once: the five-element arrays in the .2h at
-- name_end +176, +181, +186, +191 and +200 are FIVE SQUAD SLOTS, not five
-- single-round slots. Seeing five of each invited the connection and it is
-- false. Bruise leads two squads and has exactly two non-zero entries in every
-- array, with targets [8, 2] against an Army Setup screen reading large monsters
-- then archers; Guarlan leads one and has one; Viribus leads none and they are
-- all zero.
CREATE TABLE IF NOT EXISTS battle_order_options (
    kind        TEXT NOT NULL,   -- 'single_round' | 'main_order' | 'target'
    label       TEXT NOT NULL,   -- exactly as the game writes it
    position    INTEGER,         -- order within its list, top to bottom
    code        INTEGER,         -- .2h byte value, where known
    evidence    TEXT,
    observed_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (kind, label)
);

-- Model profiles: how a model wraps its reasoning, and how it is asked to call
-- tools. Configuration rather than code because every model differs and the
-- answer changes with the server build as well as the weights.
--
-- `reasoning_end` may be empty, meaning "to the next start marker or the end of
-- the text". That is not a convenience: Gemma emits `<|channel>thought` with no
-- closing marker, and treating a missing end as "no reasoning" shows the user
-- the whole chain of thought as though it were the answer.
--
-- `tool_mode` is the setting that matters most. With native tool calling a
-- local server returns content:'' beside the call — the reasoning is discarded
-- by its tool-call parser — so the run is silent. 'text' puts the tool list in
-- the prompt and makes the model write reasoning and call as prose.
--
-- An empty `system_prompt` means "use the built-in one", so the shipped prompt
-- keeps improving instead of being frozen into a row the day it was copied.
CREATE TABLE IF NOT EXISTS agent_profile (
    name            TEXT PRIMARY KEY,
    system_prompt   TEXT,
    reasoning_start TEXT NOT NULL DEFAULT '',
    reasoning_end   TEXT NOT NULL DEFAULT '',
    prefill         TEXT NOT NULL DEFAULT '',
    preserve_tool_reasoning INTEGER NOT NULL DEFAULT 1,
    tool_mode       TEXT NOT NULL DEFAULT 'auto',  -- auto | native | text
    temperature     REAL NOT NULL DEFAULT 0.3,
    max_tokens      INTEGER NOT NULL DEFAULT 1600,
    request_timeout INTEGER NOT NULL DEFAULT 300,
    notes           TEXT,
    is_active       INTEGER NOT NULL DEFAULT 0,
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Every explicit profile save is recoverable. Prompt tuning is experimental;
-- replacing the only copy makes it impossible to relate a good or bad turn to
-- the wording/settings that produced it.
CREATE TABLE IF NOT EXISTS agent_profile_revision (
    id              INTEGER PRIMARY KEY,
    profile_name    TEXT NOT NULL,
    system_prompt   TEXT NOT NULL DEFAULT '',
    reasoning_start TEXT NOT NULL DEFAULT '',
    reasoning_end   TEXT NOT NULL DEFAULT '',
    prefill         TEXT NOT NULL DEFAULT '',
    preserve_tool_reasoning INTEGER NOT NULL DEFAULT 1,
    tool_mode       TEXT NOT NULL,
    temperature     REAL NOT NULL,
    max_tokens      INTEGER NOT NULL,
    request_timeout INTEGER NOT NULL DEFAULT 300,
    notes           TEXT,
    saved_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_agent_profile_revision
    ON agent_profile_revision(profile_name, id);
-- Battle setup and equipment, recorded as intent like every other decision.
--
-- Separate tables rather than columns on order_intent because they key
-- differently: an order is one per commander per turn, a battle stance is one
-- per commander PER SQUAD, and equipment is one per commander per SLOT.
-- Folding them together would make "the latest row wins" mean three different
-- things in the same table.
CREATE TABLE IF NOT EXISTS battle_intent (
    id             INTEGER PRIMARY KEY,
    game_id        INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    turn           INTEGER NOT NULL,
    commander_id   INTEGER NOT NULL,
    commander_name TEXT,
    -- NULL means the commander's own battle order, at +198/+199. A number is
    -- a squad slot 0-4, at +186[n]/+191[n]/+200[n]. The game treats these as
    -- one vocabulary but two places, and so does this.
    squad          INTEGER,
    stance         TEXT,
    target         TEXT,
    formation      INTEGER,
    rationale      TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_battle_intent
    ON battle_intent(game_id, turn, commander_id, squad, id);

CREATE TABLE IF NOT EXISTS equipment_intent (
    id             INTEGER PRIMARY KEY,
    game_id        INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    turn           INTEGER NOT NULL,
    commander_id   INTEGER NOT NULL,
    commander_name TEXT,
    slot           TEXT    NOT NULL,   -- a key of EQUIPMENT_SLOTS
    item_id        INTEGER NOT NULL,   -- 0 empties the slot
    rationale      TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_equipment_intent
    ON equipment_intent(game_id, turn, commander_id, slot, id);

-- Latest row wins, same rule as current_orders. COALESCE on squad because
-- SQLite groups NULLs separately and the commander's own order must be one
-- key, not one per row.
CREATE VIEW IF NOT EXISTS current_battle_intent AS
SELECT b.* FROM battle_intent b
JOIN (SELECT game_id, turn, commander_id, COALESCE(squad,-1) AS sq, MAX(id) AS id
      FROM battle_intent GROUP BY game_id, turn, commander_id, COALESCE(squad,-1)) l
  ON l.id = b.id;

CREATE VIEW IF NOT EXISTS current_equipment_intent AS
SELECT e.* FROM equipment_intent e
JOIN (SELECT game_id, turn, commander_id, slot, MAX(id) AS id
      FROM equipment_intent GROUP BY game_id, turn, commander_id, slot) l
  ON l.id = e.id;

-- Change Shape is instantaneous state, not a commander order.  Keeping it in
-- its own latest-row-wins history means changing form never replaces Move,
-- Research, Forge, or any other strategic order belonging to that commander.
-- Source and target HP are stored because pretender/game modifiers make the
-- effective values differ from the raw chassis database (Mambo is 32/18 while
-- the corresponding reference rows are 25/12).
CREATE TABLE IF NOT EXISTS shape_change_intent (
    id             INTEGER PRIMARY KEY,
    game_id        INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    turn           INTEGER NOT NULL,
    commander_id   INTEGER NOT NULL,
    commander_name TEXT,
    source_type_id INTEGER NOT NULL,
    source_hp      INTEGER NOT NULL,
    target_type_id INTEGER NOT NULL,
    target_hp      INTEGER NOT NULL,
    rationale      TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_shape_change_intent
    ON shape_change_intent(game_id, turn, commander_id, id);

CREATE VIEW IF NOT EXISTS current_shape_change_intent AS
SELECT s.* FROM shape_change_intent s
JOIN (SELECT game_id, turn, commander_id, MAX(id) AS id
      FROM shape_change_intent GROUP BY game_id, turn, commander_id) l
  ON l.id = s.id;

-- The remaining battle fields have different replacement keys, so they stay
-- separate instead of overloading battle_intent: position is per squad, gems
-- are per path, and a five-round script is one atomic queue per commander.
CREATE TABLE IF NOT EXISTS battle_position_intent (
    id             INTEGER PRIMARY KEY,
    game_id        INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    turn           INTEGER NOT NULL,
    commander_id   INTEGER NOT NULL,
    commander_name TEXT,
    squad          INTEGER NOT NULL,
    x              INTEGER NOT NULL,
    y              INTEGER NOT NULL,
    rationale      TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_battle_position_intent
    ON battle_position_intent(game_id, turn, commander_id, squad, id);

CREATE TABLE IF NOT EXISTS carried_gem_intent (
    id             INTEGER PRIMARY KEY,
    game_id        INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    turn           INTEGER NOT NULL,
    commander_id   INTEGER NOT NULL,
    commander_name TEXT,
    path           INTEGER NOT NULL, -- FAWESDNGB index
    amount         INTEGER NOT NULL, -- desired carried total, not a delta
    rationale      TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_carried_gem_intent
    ON carried_gem_intent(game_id, turn, commander_id, path, id);

CREATE TABLE IF NOT EXISTS battle_script_intent (
    id             INTEGER PRIMARY KEY,
    game_id        INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    turn           INTEGER NOT NULL,
    commander_id   INTEGER NOT NULL,
    commander_name TEXT,
    queue_json     TEXT NOT NULL, -- complete ordered list, up to five entries
    rationale      TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_battle_script_intent
    ON battle_script_intent(game_id, turn, commander_id, id);

CREATE VIEW IF NOT EXISTS current_battle_position_intent AS
SELECT p.* FROM battle_position_intent p
JOIN (SELECT game_id, turn, commander_id, squad, MAX(id) AS id
      FROM battle_position_intent
      GROUP BY game_id, turn, commander_id, squad) l
  ON l.id = p.id;

CREATE VIEW IF NOT EXISTS current_carried_gem_intent AS
SELECT g.* FROM carried_gem_intent g
JOIN (SELECT game_id, turn, commander_id, path, MAX(id) AS id
      FROM carried_gem_intent
      GROUP BY game_id, turn, commander_id, path) l
  ON l.id = g.id;

CREATE VIEW IF NOT EXISTS current_battle_script_intent AS
SELECT s.* FROM battle_script_intent s
JOIN (SELECT game_id, turn, commander_id, MAX(id) AS id
      FROM battle_script_intent GROUP BY game_id, turn, commander_id) l
  ON l.id = s.id;

-- Troop assignment. One current destination per unit instance; a single tool
-- call may insert several rows atomically. Creating an empty commander slot is
-- a separate intent because its collision-free squad id must remain stable
-- across every later materialisation of this turn.
CREATE TABLE IF NOT EXISTS troop_assignment_intent (
    id                  INTEGER PRIMARY KEY,
    game_id             INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    turn                INTEGER NOT NULL,
    unit_instance_id    INTEGER NOT NULL,
    unit_type_id        INTEGER NOT NULL,
    unit_name           TEXT,
    destination         TEXT NOT NULL DEFAULT 'squad',
    -- 0/-1 are internal sentinels when destination='garrison'. Keeping these
    -- columns non-null preserves compatibility with databases created before
    -- garrison detachment was added.
    target_commander_id INTEGER NOT NULL,
    commander_name      TEXT,
    target_squad        INTEGER NOT NULL,
    rationale           TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_troop_assignment_intent
    ON troop_assignment_intent(game_id, turn, unit_instance_id, id);

CREATE VIEW IF NOT EXISTS current_troop_assignment_intent AS
SELECT a.* FROM troop_assignment_intent a
JOIN (SELECT game_id, turn, unit_instance_id, MAX(id) AS id
      FROM troop_assignment_intent
      GROUP BY game_id, turn, unit_instance_id) l
  ON l.id = a.id;

CREATE TABLE IF NOT EXISTS squad_creation_intent (
    id                  INTEGER PRIMARY KEY,
    game_id             INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    turn                INTEGER NOT NULL,
    target_commander_id INTEGER NOT NULL,
    commander_name      TEXT,
    target_squad        INTEGER NOT NULL,
    squad_id            INTEGER NOT NULL,
    rationale           TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_squad_creation_intent
    ON squad_creation_intent(
        game_id, turn, target_commander_id, target_squad, id);

CREATE VIEW IF NOT EXISTS current_squad_creation_intent AS
SELECT s.* FROM squad_creation_intent s
JOIN (SELECT game_id, turn, target_commander_id, target_squad, MAX(id) AS id
      FROM squad_creation_intent
      GROUP BY game_id, turn, target_commander_id, target_squad) l
  ON l.id = s.id
WHERE EXISTS (
    SELECT 1 FROM current_troop_assignment_intent a
    WHERE a.game_id = s.game_id
      AND a.turn = s.turn
      AND a.destination = 'squad'
      AND a.target_commander_id = s.target_commander_id
      AND a.target_squad = s.target_squad
);
-- What a province's panel shows: income, resources, recruitment points and
-- supplies. NOT stored in any save file — searched exhaustively in the .trn,
-- ftherlnd and the .map against real readings — so they are recorded here when
-- read off the screen, exactly as observed_unit_costs was.
--
-- Kept per turn rather than per province, because they move: resources rise
-- and fall with population, order, production and unrest, so a figure is only
-- true of the turn it was read on. A stale one is worse than none, and the
-- turn column is what lets a reader tell.
CREATE TABLE IF NOT EXISTS province_economics (
    province_id      INTEGER NOT NULL,
    turn             INTEGER NOT NULL,
    income           INTEGER,
    resources        INTEGER,
    recruit_points   INTEGER,
    commander_points INTEGER,
    supplies         INTEGER,
    source           TEXT,      -- 'province panel', usually
    observed_at      TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (province_id, turn)
);

-- Recruitment recorded as intent, like every other decision. Over-queueing is
-- deliberate and allowed: a province builds what it can afford and carries the
-- rest to next turn, which is a way of pre-committing to an expensive unit.
CREATE TABLE IF NOT EXISTS recruit_intent_v2 (
    id           INTEGER PRIMARY KEY,
    game_id      INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    turn         INTEGER NOT NULL,
    province_id  INTEGER NOT NULL,
    position     INTEGER NOT NULL,   -- build order within the province
    unit_type_id INTEGER NOT NULL,
    kind         TEXT CHECK(kind IN ('commander', 'troop')),
    gold         INTEGER,            -- what will be written into the .2h
    rationale    TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_recruit_intent_v2
    ON recruit_intent_v2(game_id, turn, province_id, position);

-- Nation-level research queue. One row records one complete decision, rather
-- than one row per school, because replacing/reordering the queue is atomic and
-- the latest complete queue must win. queue_json is an array of school ids in
-- execution order. Values 0-6 are school ids; repeating one researches that
-- school's following level. Larger values are direct Level 9 spell ids. The
-- tool validates that each spell's school has reached Level 8 earlier in the
-- queue, so levels cannot be skipped.
CREATE TABLE IF NOT EXISTS research_intent (
    id         INTEGER PRIMARY KEY,
    game_id    INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    turn       INTEGER NOT NULL,
    queue_json TEXT    NOT NULL,
    rationale  TEXT    NOT NULL,
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_research_intent
    ON research_intent(game_id, turn, id);

CREATE VIEW IF NOT EXISTS current_research_intent AS
SELECT r.* FROM research_intent r
JOIN (SELECT game_id, turn, MAX(id) AS id
      FROM research_intent GROUP BY game_id, turn) latest
  ON latest.id = r.id;

-- One complete province-defence target per decision. The .2h stores the
-- desired level rather than a delta; latest intent wins independently in each
-- province, while earlier rationales remain available for audit.
CREATE TABLE IF NOT EXISTS province_defence_intent (
    id          INTEGER PRIMARY KEY,
    game_id     INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    turn        INTEGER NOT NULL,
    province_id INTEGER NOT NULL,
    target      INTEGER NOT NULL,
    rationale   TEXT    NOT NULL,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_province_defence_intent
    ON province_defence_intent(game_id, turn, province_id, id);

CREATE VIEW IF NOT EXISTS current_province_defence_intent AS
SELECT p.* FROM province_defence_intent p
JOIN (SELECT game_id, turn, province_id, MAX(id) AS id
      FROM province_defence_intent
      GROUP BY game_id, turn, province_id) latest
  ON latest.id = p.id;

-- One standing mercenary bid per auction slot. The slot is the company's
-- position in the auction list; `amount` NULL withdraws a bid, which is how a
-- replacement refunds the previous reservation instead of double-charging.
CREATE TABLE IF NOT EXISTS mercenary_bid_intent (
    id           INTEGER PRIMARY KEY,
    game_id      INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    turn         INTEGER NOT NULL,
    slot         INTEGER NOT NULL,
    company      TEXT    NOT NULL,
    amount       INTEGER,
    province_id  INTEGER,
    -- Nation-specific minimum copied from the player's hire screen. The .trn
    -- stores only the lower asking price and cannot derive this safely.
    quoted_minimum INTEGER,
    rationale    TEXT    NOT NULL,
    created_at   TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_mercenary_bid_intent
    ON mercenary_bid_intent(game_id, turn, slot, id);

CREATE VIEW IF NOT EXISTS current_mercenary_bid_intent AS
SELECT m.* FROM mercenary_bid_intent m
JOIN (SELECT game_id, turn, slot, MAX(id) AS id
      FROM mercenary_bid_intent
      GROUP BY game_id, turn, slot) latest
  ON latest.id = m.id;

-- One outgoing diplomatic decision per target nation. `clear` deliberately
-- remains a real intent row: latest-row-wins must be able to remove a proposal
-- or declaration already present in the pristine orders file.
CREATE TABLE IF NOT EXISTS diplomacy_intent (
    id               INTEGER PRIMARY KEY,
    game_id          INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    turn             INTEGER NOT NULL,
    target_nation_id INTEGER NOT NULL,
    action           TEXT    NOT NULL, -- propose/accept/decline NAP | declare_war | clear
    missive          TEXT,
    rationale        TEXT    NOT NULL,
    created_at       TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_diplomacy_intent
    ON diplomacy_intent(game_id, turn, target_nation_id, id);

CREATE VIEW IF NOT EXISTS current_diplomacy_intent AS
SELECT d.* FROM diplomacy_intent d
JOIN (SELECT game_id, turn, target_nation_id, MAX(id) AS id
      FROM diplomacy_intent
      GROUP BY game_id, turn, target_nation_id) latest
  ON latest.id = d.id;

-- SillyTavern-compatible character cards are a harness preference, not game
-- state. Bindings use opaque scope keys (turn:<game id> and
-- pretender:<nation id>) so pregame design can use the same library before a
-- game row exists. The untouched source JSON is retained per revision while
-- only normalized, bounded fields are injected into model context.
CREATE TABLE IF NOT EXISTS character_card (
    id                INTEGER PRIMARY KEY,
    name              TEXT NOT NULL,
    spec              TEXT NOT NULL,
    spec_version      TEXT NOT NULL,
    raw_json          TEXT NOT NULL,
    source_blob       BLOB,
    source_sha256     TEXT NOT NULL UNIQUE,
    source_filename   TEXT,
    source_kind       TEXT NOT NULL,
    avatar_path       TEXT,
    creator           TEXT,
    character_version TEXT,
    creator_notes     TEXT,
    tags_json         TEXT NOT NULL DEFAULT '[]',
    enabled           INTEGER NOT NULL DEFAULT 1,
    imported_at       TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS character_card_revision (
    id          INTEGER PRIMARY KEY,
    card_id     INTEGER NOT NULL REFERENCES character_card(id) ON DELETE CASCADE,
    raw_json    TEXT NOT NULL,
    avatar_path TEXT,
    saved_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_character_revision_card
    ON character_card_revision(card_id, id);

CREATE TABLE IF NOT EXISTS character_book_entry (
    id                  INTEGER PRIMARY KEY,
    revision_id         INTEGER NOT NULL REFERENCES character_card_revision(id) ON DELETE CASCADE,
    entry_index         INTEGER NOT NULL,
    name                TEXT,
    keys_json           TEXT NOT NULL DEFAULT '[]',
    secondary_keys_json TEXT NOT NULL DEFAULT '[]',
    content             TEXT NOT NULL,
    enabled             INTEGER NOT NULL DEFAULT 1,
    insertion_order     INTEGER NOT NULL DEFAULT 0,
    priority            INTEGER NOT NULL DEFAULT 0,
    constant            INTEGER NOT NULL DEFAULT 0,
    selective           INTEGER NOT NULL DEFAULT 0,
    case_sensitive      INTEGER NOT NULL DEFAULT 0,
    position            TEXT NOT NULL DEFAULT 'after_char',
    extensions_json     TEXT NOT NULL DEFAULT '{}',
    UNIQUE(revision_id, entry_index)
);

CREATE TABLE IF NOT EXISTS character_binding (
    scope_key      TEXT PRIMARY KEY,
    card_id        INTEGER REFERENCES character_card(id) ON DELETE SET NULL,
    revision_id    INTEGER REFERENCES character_card_revision(id) ON DELETE SET NULL,
    influence_mode TEXT NOT NULL DEFAULT 'strategy_and_voice',
    updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS character_injection_run (
    id                    INTEGER PRIMARY KEY,
    scope_key             TEXT NOT NULL,
    card_id               INTEGER,
    revision_id           INTEGER,
    influence_mode        TEXT,
    query_text            TEXT,
    matched_lore_ids_json TEXT NOT NULL DEFAULT '[]',
    rendered_chars        INTEGER NOT NULL DEFAULT 0,
    created_at            TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_character_injection_scope
    ON character_injection_run(scope_key, id);

-- Named assistant chats are independent of model/game state but scoped to one
-- Pretender nation or live player view. Browser storage is only a cache; the
-- complete transparent history and Activity transcript live here.
CREATE TABLE IF NOT EXISTS assistant_conversation (
    id         TEXT PRIMARY KEY,
    scope_key  TEXT NOT NULL,
    title      TEXT NOT NULL,
    state_json TEXT NOT NULL DEFAULT '{"history":[],"transcript":[]}',
    archived   INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_assistant_conversation_scope
    ON assistant_conversation(scope_key, archived, updated_at);

-- Fields the reverse-engineering work has established we cannot read, and what
-- experiment would settle each. Created by hand during decoding and never in
-- the schema, so every fresh install had the table missing and the /gaps view
-- returned a 500 instead of an empty list. The rows are findings rather than
-- game state: a new install starts empty and reports nothing recorded, which
-- is the honest answer rather than a fabricated one.
CREATE TABLE IF NOT EXISTS decode_status (
    field        TEXT PRIMARY KEY,
    outcome      TEXT NOT NULL,
    finding      TEXT NOT NULL,
    unblock_test TEXT,
    recorded_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
