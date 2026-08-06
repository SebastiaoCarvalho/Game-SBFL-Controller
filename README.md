# Experiment Controller

Automated controller for the GameRV fault-localization experiment. It is
**multi-game**: each row of `dataset.csv` names a **Game** (the `Game Name`
column), and for that row the controller builds the mutated game branch, verifies
the row's safety property with `gamerv`, and checks whether the ground-truth
refinement `phi` entails the mined refinement `R` (`phi -> R`). A row is
**SUCCESS** when `phi -> R`, **FAIL** otherwise.

The nine-step flow is identical for every game — a mutant is always a git branch
you compile, and a trace is always a `.rec` tape replayed through the game's
`--replay` hook. Only the game-specific constants differ, and those live in one
`config.Game` bundle per game (see [Games](#games)).

## Layout

The project root holds only the `run_experiment.sh` entrypoint and the data
directories it writes (`results/`, `hints/`, `log_cache/`, `build_cache/`,
`logs_archive/`). All Python modules live in `src/`; workflow helper scripts
that are not part of the pipeline itself live in `scripts/`.

| File | Role |
|------|------|
| `run_experiment.sh` | Root entrypoint: thin wrapper that forwards its args to `src/run_experiment.py`. |
| `src/config.py` | Shared paths/constants **and** the per-game `Game` registry (`GAMES`, `get_game()`). |
| `src/dataset.py` | Reads `dataset.csv` into `TestCase(game, specification, branch, phi)`. |
| `src/game_repo.py` | git checkout, build, game launch/shutdown — each fn takes a `config.Game`. |
| `src/instrument.py` | Step 2a: runs gamerv's source instrumenter over the branch before each compile, then reverts it. |
| `src/gamerv.py` | Wrappers around gamerv's `worker.py` ops + logs/properties/active-game helpers. |
| `src/manual_review.py` | Operator prompts for gamerv's manual visual/audio predicate values (step 6). |
| `src/hints.py` | Caches the operator's manual answers per recording (`hints/`) so `--manual-auto` can replay them. |
| `src/recordings.py` | Lists a game/branch's `.rec` tapes and computes a replay's duration from its frame count. |
| `src/pipeline.py` | `run_case()` — the 9-step workflow incl. `record_traces` (step 5). |
| `src/run_experiment.py` | CLI implementation: select rows, loop, write `results/`. |
| `scripts/prepare_branches.py` | **Secondary** maintenance: reconcile a game's branches with its `base`. |

## Games

Everything game-specific is a frozen `config.Game` bundle, selected by the row's
`Game Name` via `config.get_game(name)` (case-insensitive; raises
`UnknownGameError` for a blank/unregistered name → the row becomes an `ERROR`).
The roster is `config.GAMES`; **add a game by appending one `Game` to
`config._GAMES`**. Fields: `repo_dir`, `build_dir`, `binary`, `base_branch`,
`build_commands` (cmake configure+build for SuperTux; a game on another build
system supplies its own command list), `cache_artifacts`, `rec_dir`,
`launch_args`, `replay_flag` / `replay_delay_flag` / `replay_delay` /
`replay_margin`, `seconds_per_step`, and `baseline_contrast_branches`.

**gamerv catalog selection.** gamerv keeps a per-game catalog bundle in
`config/games/<name>/` (`game_profile.json` + `event_types.json`) and picks the
active one from the `GAME` env var (which overrides its `config/active_game.txt`).
For each row the controller sets `GAME=<Game Name>` for the whole `run_case`
(restored afterwards), so every worker op — `check_formula`, `log`, `validate`,
`compare_refinement` — loads that game's predicate catalog. Before touching any
game state it preflights `gamerv.available_games()` and `ERROR`s the row if the
game has no gamerv bundle. **Contract: a dataset `Game Name` must exactly equal
the gamerv `config/games/<name>` folder** (today both are `Super Tux`).

Caches and hints are namespaced by game — `build_cache/<game>/<branch>/`,
`log_cache/<game>/<branch>/`, `hints/<game>/<branch>/` — so two games can share a
branch name (e.g. `base`) without colliding.

## The 9 steps (`pipeline.run_case`)

1. Read the dataset row (and resolve its `Game`).
2. Checkout the branch, **instrument its source (step 2a)**, and build
   (`game.build_commands`). The game is launched per-tape in step 5, since the
   replay is triggered by a launch-time flag.
3. Set the spec (`G(<Specification>)`) as the **only** enabled gamerv property —
   guarded by `check_formula`, so an unaligned row fails loudly.
4. Clear `gamerv/logs/`.
5. **Record traces.** For each `.rec` tape in `<game.rec_dir>/<branch>/`: launch
   the game with `<replay_flag> <tape> <replay_delay_flag> <s>`, start gamerv
   logging (which attaches to the window and clears the event channel), let the
   game auto-fire the replay, wait for it to finish (duration = frames ×
   `game.seconds_per_step`), then SIGINT the logger. Traces are **not** validated
   here.
6. Validate every recorded run (`worker.py validate`). If `MANUAL_PREDICATES`
   is on (see below), validate pauses for operator judgement — handled inline.
7. Select the last invalid trace. (Fault localization is intentionally skipped —
   `compare_refinement` re-mines `R` itself, so a separate pass would duplicate it.)
8. Decide whether `phi` is a valid refinement (`worker.py compare_refinement`) —
   symbolic `phi -> R` or data-direct, per `--refinement-mode` (see below).
9. Emit SUCCESS / FAIL (plus NOT_FOUND / INCONCLUSIVE / ERROR for diagnostics).

Refinement mining is **generic** — gamerv auto-discovers candidate atoms from
every logged variable. (The old per-row `Target Variables` "box-only" vocabulary
and its `--use-target-variables` flag have been removed.)

#### Step 2a — source instrumentation (local variables)

Mining can only refine over variables the snapshot actually carries, and a
function-local is invisible unless the source reports it. So **before every
compile** the controller runs gamerv's source instrumenter over the checked-out
tree (`runners.source_instrument`, gamerv branch `local_vars`): it parses the game
with libclang, selects the locals worth reporting, and splices the `REPORT_LOCAL`
calls in. The compiler then sees the instrumented source, and **the edit is
reverted as soon as the build finishes** — pass or fail — so the repo is clean for
the next checkout and the instrumentation never lands on a dataset branch.

It lives inside `game_repo.build()` rather than beside it in `run_case`, because
it has to wrap *every* compile: a baseline-contrast row builds twice (base and
mutant, from step 5) and both must be instrumented identically or their traces
would not share a vocabulary. Its cost shows up in the `build` phase timing.

- **Per game, opt-in:** `Game.instrument_src` (the source root to rewrite);
  `None` skips the step. On for **Super Tux** only today. A game can turn it on
  once (a) gamerv resolves an instrumentation spec for it —
  `config/games/<name>/source_instrument.json`, since gamerv's built-in default
  spec scans SuperTux paths — and (b) its `base` carries the reporting runtime
  (the `REPORT_LOCAL` macros), or the instrumented tree will not compile.
  `Game.instrument_extra_args` passes extra flags (e.g.
  `("--max-keys-per-class", "8")`).
- **Container variables.** A local that is a `std::vector`, a `std::array` or a
  C array is reported as one key whose value is a JSON array
  (`REPORT_LOCAL_ARRAY`), which mining reads both whole and through paths into it
  (`player.tiles[2] > 3`). This needs the *newer* runtime: SuperTux's `base`
  carries those macros as of its "add array variables" commit, and the spec opts
  in with `"array"` in `policy.emit_verdicts`. **Per branch, not per game** — each
  dataset branch carries its own copy of `local_report.hpp`, so the instrumenter
  probes the checkout it is about to write into and drops the container forms
  (loudly, on stdout) where the macros are absent, instead of writing a call that
  cannot compile and failing the whole row. Today that means only `base` reports
  containers; `python scripts/prepare_branches.py --game "Super Tux"` rebases the
  mutation branches onto it, after which base and mutant share the vocabulary
  again. One array key costs its whole element list in **every** snapshot, so
  length is what is budgeted, not key count (`policy.max_container_elements`).
- **Off switch:** `--no-instrument` (or `config.INSTRUMENT = False`) builds each
  branch as committed. Useful to A/B what the local variables actually buy.
- **A failure fails the row** (`ERROR`) instead of quietly building an
  uninstrumented binary — results that silently lack the variables the experiment
  is about are worse than a loud row. If the instrumenter cannot be found (a
  gamerv checkout without `src/runners/source_instrument/`), the message says so.
- **Manifests** go to `instrument/<game>.manifest.json` *outside* the game repo —
  the tool's own default is `<game-root>/.gamerv/`, whose untracked directory
  would trip `ensure_clean`. They are deleted on revert; a stale one (left by a
  crash) is cleared on the next instrumented build.
- **Recovery.** If a crash ever leaves a tree instrumented, `ensure_clean` aborts
  the next run with the diff in hand; `git -C <game-repo> checkout -- .` restores
  it (the edits are never anything you wrote).

#### Refinement decision path and threshold strategy (step 8)

How step 8 decides `phi`, and where `R`'s split-points land, are configurable
(`config.REFINEMENT_MODE` / `config.THRESHOLD_STRATEGY`, each overridable with a
CLI flag):

- `--refinement-mode {auto,data-direct,symbolic}` (default **symbolic**):
  - `symbolic` — force the symbolic `phi -> R` containment check against the
    mined `R`. Verdict reads *"phi IS / is NOT CONTAINED in R"*. `R`'s thresholds
    matter here, so this is the mode that the threshold strategy below affects.
    When `R` does not establish `phi` but the data-direct test does, the row is
    classified **NOT_FOUND** (the mined `R` missed a real refinement).
  - `data-direct` — evaluate `phi` straight on the pooled good (`P & Q`) and
    violation (`P & ~Q`) states, independent of `R`. Verdict reads *"phi IS a
    sound refinement, corroborated by the data"*. Errors on a `phi` that reads a
    non-raw catalogue predicate (it has no raw value to evaluate).
  - `auto` — data-direct first, symbolic only as a fallback for non-raw `phi`
    (gamerv's historical behaviour).
- `--threshold-strategy {midpoints,weakest,gap-mean}` (default **weakest**) —
  how each numeric atom's split-point in `R` is placed between the violation and
  good values. **Only affects the symbolic path** (the data-direct verdict does
  not depend on `R`'s threshold):
  - `weakest` — the loosest sound separator: `> max(violation)` / `<
    min(violation)`. The most permissive `R`; symbolic `phi -> R` accepts the
    most `phi`s.
  - `gap-mean` — the midpoint of the violation→nearest-good gap. A stricter `R`.
  - `midpoints` — sample midpoints across the pooled values and let the mining
    search pick (gamerv's historical behaviour).

### Baseline-contrast rows (record on `base` + mutant)

Most mutants break their property only under some condition, so a single mutant
trace contains both good (`P & Q`) and violation (`P & ~Q`) states and mining can
contrast them. A few mutations are **always-on** for the level their property
covers — e.g. `change_music_config` plays the wrong music on *every* entry to its
level — so the mutant trace has only violations and no good states to contrast,
and mining returns `no_positives` (reported INCONCLUSIVE).

For the branches listed in the game's `baseline_contrast_branches`, step 5 instead
records the row's tapes on **both** `base` and the mutant into the one `logs/`
dir (step 4 still clears once, with no clear in between). `base` supplies the
valid `P & Q` states, the mutant the `P & ~Q` violations, and
`compare_refinement` pools every trace under `logs/` (`build_mining_pool`), so
phi is decided across the branch contrast: `base` validates clean, the mutant
violates, and phi is proven a sound refinement on the pooled good/violation
states. No gamerv change is needed — its pool is already cross-trace. The same
tapes drive both binaries, so only the code under test differs. Step 2 defers its
build for these rows since step 5 builds `base` and the mutant in turn.

### Step 5 needs a game-side `--replay` hook (on `base`)

Trace recording relies on the game being able to boot a level and auto-fire a
recorded `.rec` tape. For SuperTux, `replay_recording` is otherwise console-only,
so `base` was given a startup hook: a `--replay <rec>` flag (with `--replay-delay
<seconds>`) that boots the recorded level and auto-fires the replay after the
delay — long enough for the logger to attach and clear its channel first. Files
touched on SuperTux: `command_line_arguments.{hpp,cpp}`, `gameconfig.{hpp,cpp}`,
`main.cpp`, `game_session.cpp`. **Commit these to the game's `base`**, then
`scripts/prepare_branches.py` propagates them to that game's dataset branches.
A new game needs an equivalent hook, and its `Game` bundle records the flag names
(`replay_flag` / `replay_delay_flag`).

The logger attaches to the game window via the GNOME screen-capture portal,
which prompts once to pick the source; after that a restore token makes it
sub-second. The first run is therefore interactive (pick the game/monitor).

## Usage

```bash
# Run the whole pipeline over dataset rows (uses the same `python` as gamerv):
./run_experiment.sh                    # all rows
./run_experiment.sh --row 4            # one row
./run_experiment.sh --rows 4-9         # a range
./run_experiment.sh --game "Super Tux" # only that game's rows
./run_experiment.sh --row 4 --manual-auto   # replay cached manual answers
./run_experiment.sh --row 20 --refinement-mode data-direct --threshold-strategy weakest
./run_experiment.sh --row 4 --no-instrument  # skip step 2a (build source as committed)
```

Results are written to `results/results_<timestamp>.csv` (with a `game` column)
and summarized on stdout.

`--manual-auto` replays previously recorded answers to the manual image/audio
predicate prompts (see *Manual predicate values* below) so a re-run doesn't ask
again.

## Prerequisites (owned outside the controller)

1. **Clean game tree.** The controller **aborts if the game's tree is dirty**.
   A game's `base` typically has uncommitted edits — its instrumentation plus the
   `--replay` hook. Commit them before running.
2. **gamerv catalog.** The game must have a gamerv bundle at
   `config/games/<Game Name>/` (matching the dataset name exactly), or the row is
   `ERROR`ed at preflight.
3. **Dataset alignment.** `Specification` / `Target Phi` must use that game's
   exact gamerv catalog vocabulary (`PlayerWalked`, `ObjectPresent`,
   `VisualAssertion`, raw atoms like `player.hurt == false`). The controller feeds
   specs verbatim — it does not translate names. Misaligned rows are reported as
   `ERROR` with the parser message.
4. **Branch reconciliation.** Dataset branches must contain `base`'s
   instrumentation **and** the `--replay` hook. After committing the `base`
   changes, run the secondary script (it reports a branch current only when
   `base` is an ancestor of it):

   ```bash
   python scripts/prepare_branches.py --game "Super Tux" --dry-run  # what needs work
   python scripts/prepare_branches.py --game "Super Tux"            # rebase onto base
   ```

   `--game` defaults to the only registered game when there is exactly one.
   Conflicts are expected on diverged branches; those are reported `NEEDS-MANUAL`
   and must be rebased by hand. The rebase rewrites history vs origin — push with
   `--force-with-lease` if/when you choose.
5. **Source instrumenter (step 2a), for games that enable it.** The gamerv
   checkout must be on a branch carrying `src/runners/source_instrument/` (today:
   `local_vars`) and its libclang must import in the `gamerv` conda env — the
   controller runs `python -m runners.source_instrument` from `gamerv/src/`. A
   gamerv checkout without it fails every row of an instrumented game with a
   message saying so; `--no-instrument` builds without it in the meantime.

## Notes

- **Build/GLOB caveat (SuperTux).** SuperTux gathers sources with `file(GLOB)` and
  no `CONFIGURE_DEPENDS`, so a build dir configured for one branch silently drops
  files added/removed/renamed on another → link errors. Its `build_commands` re-run
  cmake configure every build to re-glob. If a build still fails to link after
  switching far-diverged branches, wipe `<game.build_dir>/` for a clean configure.
- **Build cache (`build_cache/<game>/`).** `game_repo.build()` caches the compiled
  binary per game+branch, keyed by the HEAD **tree hash** (`git rev-parse
  HEAD^{tree}`) — an exact content key for the checked-out source (sound because
  `ensure_clean` aborts a dirty run, so the tree hash captures every tracked file)
  — **plus the digest of the pending step-2a instrumentation** (`<tree>+i<digest>`),
  since those edits are what actually get compiled and they never move the tree
  hash. `instrument.plan()` re-derives that digest on every build without writing
  anything (a few seconds), so a change to the instrumenter or its spec misses the
  cache and rebuilds instead of restoring a binary built with the previous
  instrumentation. Turning `--no-instrument` on or off flips the key the same way.
  On a matching key the configure+build is skipped and the cached binary is
  restored; a source change yields a different hash → miss → rebuild
  (self-correcting, like the log cache). Each slot snapshots just the game binary
  and the build-local libs it loads (`game.cache_artifacts`), not the whole build
  dir, and is one-per-branch so disk stays bounded. Delete `build_cache/<game>/
  <branch>/` (or the whole dir) to force a rebuild.
- **Log cache (`log_cache/<game>/`).** Step 5's raw logs depend only on the binary
  and the row's tapes, so a content-hash hit restores the pristine run dirs and
  re-validates instead of re-recording. A changed binary/tape → miss → re-record.
- `gamerv validate` invokes LLM vision (keys in `gamerv/.env`) + DeepFace and
  needs a display for capture — heavyweight.
- **Manual predicate values.** When `gamerv/.env` sets `MANUAL_PREDICATES`
  (`true`, or a list like `audio`/`visual,audio`), gamerv does **not** compute
  the visual (LLM) and audio predicates — it dumps the frames/audio it would
  have judged and asks for them. The controller answers this inline in step 6
  (`manual_review.collect`): for each undecided predicate it opens the frame/clip
  (`xdg-open`) and reads a `t`/`f` at the terminal, saves the answers, and
  re-runs validate until the verdict is written. This only triggers for rows
  whose property actually references a visual/audio predicate; everything else
  validates untouched. **Requires an interactive run** — with no TTY (CI /
  detached) a row that needs manual input is reported `ERROR` (`NoTTYError`).
  Leave `MANUAL_PREDICATES` empty/unset to keep validation fully automatic.
- **Replaying manual answers (`hints/`).** Every answer the operator gives is
  cached under `hints/<game>/<branch>/<recX>.hint` — one file per `.rec`
  recording, keyed by gamerv's stable `"<state_index>::<call>"` (baseline-contrast
  rows tag base vs mutant traces as `base__<recX>` / `<mutant>__<recX>` so they
  don't collide). Caching happens on *every* interactive run, so the cache builds
  itself. Pass `--manual-auto` to **replay** it: each recording's cached answers
  are merged into its `manual_answers.json` before validation, so gamerv only
  asks about predicates not yet recorded (or nothing at all). This is sound only
  because a replay of the same tape on the same build is deterministic — same
  frames → same events → same state index — so the cached keys still line up.
  Re-record a tape or change the build and any shifted predicate is simply
  re-prompted (and re-cached). `hints/` is a plain answer cache; delete a file
  (or a key inside it) to force that recording (or predicate) to be asked again.
- **Per-call defaults (reuse an answer across states/traces).** Because the
  `<state_index>::<call>` keys are per-trace, hand-writing or copying them between
  traces is fragile (the indices rarely line up). When a predicate's answer is
  *uniform* across a trace — same value at every state it's evaluated — declare it
  once as a **bare-call key** (no `::`), e.g. `{"ObjectPresent(\"Tux\")": true}`.
  A bare-call entry answers that call at every state, independent of the index, so
  it survives re-recording and is reusable across traces. Put it either inside a
  tape's own `.hint` (applies to that recording) or, to cover every tape of a
  branch at once, in `hints/<game>/<branch>/_defaults.hint` (a tape's bare-call key
  overrides the branch default for that call). With `--manual-auto`, each of
  gamerv's requests is auto-answered from these defaults and only calls no default
  covers are prompted — so a fully-defaulted row needs no TTY. Defaults are the
  single source of truth: they are *not* baked into the per-state cache, so
  editing the default changes every trace. Use them for uniform predicates;
  a predicate whose truth varies state-to-state still needs the collected
  per-state keys. `scripts/hint_defaults.py --game <name> [--all]` reports/writes
  them.
- `compare_refinement` treats non-raw `phi` atoms (e.g. `~DiedBySpikes`,
  `PocketEmpty`) as opaque booleans, so `phi -> R` typically holds only when
  `phi` is a raw-variable atom. Aligning `Target Phi` to raw atoms is the lever.
