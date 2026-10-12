# Experiment Controller

Automated controller for the GameRV fault-localization experiment. It is
**multi-game**: each row of `dataset_bug_condition.csv` names a **Game** (the
`Game Name` column), and for that row the controller builds the mutated game
branch, verifies the row's safety property with `gamerv`, ranks the **bug
conditions** gamerv mines from the traces — spectrum-based fault localization,
every candidate condition scored by its Ochiai suspiciousness — and checks the
row's ground-truth bug condition `phi` against the top of that ranking. A row is
**SUCCESS** when `phi` ties the top score and **some single top-ranked `B`**
satisfies `phi -> B`, **FAIL** when the data ranks another condition above `phi`
(or `phi` explains no violation).

The quantifier matters, and it is the evaluation rule: mining returns a whole
ranked set, and the test is over its top tier `T = B1, B2, …`:

    ∃ B ∈ T . phi -> B

not `phi -> (B1 | B2 | … | Bn)` against their union. A union covers `phi` as soon
as several candidates *together* do, while no single condition the tool would
report actually implies it — that ground truth was not found, so it does not
count. Rows in that position are reported separately (the `union_only` column and
the report's `union` count), which is what makes the stricter rule's effect
visible instead of hidden. The direction is `phi -> B` because each numeric
candidate is spelled at the loosest edge of its data gap, so a hand-written `phi`
that splits the data in the same place lies inside it.

The nine-step flow is identical for every game — a mutant is always a git branch
you compile, and a trace is always a `.rec` tape replayed through the game's
`--replay` hook. Only the game-specific constants differ, and those live in one
`config.Game` bundle per game (see [Games](#games)).

## Layout

The project root holds only the `run_experiment.sh` entrypoint and the data
directories it writes (`results/`, `hints/`, `log_cache/`, `build_cache/`,
`build_logs/`, `logs_archive/`). All Python modules live in `src/`; workflow helper scripts
that are not part of the pipeline itself live in `scripts/`.

| File | Role |
|------|------|
| `run_experiment.sh` | Root entrypoint: thin wrapper that forwards its args to `src/run_experiment.py`. |
| `src/config.py` | Shared paths/constants **and** the per-game `Game` registry (`GAMES`, `get_game()`). |
| `src/dataset.py` | Reads the dataset (`dataset_bug_condition.csv`) into `TestCase(game, specification, branch, phi, fault_type)` **and** owns the fault taxonomy (the twelve `Fault Type` categories and their two parents). |
| `src/game_repo.py` | git checkout, build, game launch/shutdown — each fn takes a `config.Game`. |
| `src/instrument.py` | Step 2a: runs gamerv's source instrumenter over the branch before each compile, then reverts it. |
| `src/term_rules.py` | Step 7a: regenerates the game's compound-term allow-list by digesting its own source, so the compound sweep is scoped per game instead of by a stale Super Tux file. |
| `src/progress.py` | Renders a long subprocess (the compiles) as one self-updating progress line, with its full output kept in `build_logs/`. |
| `src/gamerv.py` | Wrappers around gamerv's `worker.py` ops + logs/properties/active-game helpers. |
| `src/manual_review.py` | Operator prompts for gamerv's manual visual/audio predicate values (step 6). |
| `src/hints.py` | Caches the operator's manual answers per recording (`hints/`) so `--manual-auto` can replay them. |
| `src/recordings.py` | Lists a game/branch's `.rec` tapes and computes a replay's duration from its frame count. |
| `src/pipeline.py` | `run_case()` — the 9-step workflow incl. `record_traces` (step 5). |
| `src/metrics.py` | Step 8's mining metrics per row: candidate-set size, the ground truth's rank in it, the match tier, and the `union_only` flag. |
| `src/report.py` | Writes `report.txt` from a run's CSVs: classification counts, the same counts per game / bug category / parent category, the **per-game breakdown** (the whole analysis again over each game's own rows), and the per-case division (one line per dataset row); also a standalone CLI. |
| `src/plots.py` | Draws a run's figures from its CSVs (candidate-count boxplot; candidates vs. ground-truth rank); also a standalone CLI. |
| `src/compare.py` | Diffs **two** runs' `full.csv`: which rows gained or lost a SUCCESS, the counts per game/category side by side, and how the mining numbers and the ground truth's **rank** moved (top-k, MRR, rank share, per-case moves); also a standalone CLI. |
| `src/run_experiment.py` | CLI implementation: select rows, loop, write `results/`. |
| `scripts/prepare_branches.py` | **Secondary** maintenance: reconcile a game's branches with its `base`. |
| `scripts/profile_shape_lint.py` | Measures each game's object cardinality in its own traces and checks it against the role `game_profile.json` declares — which quantified collections are really singletons, and which singletons no base matches at all. |

## Games

Everything game-specific is a frozen `config.Game` bundle, selected by the row's
`Game Name` via `config.get_game(name)` (case-insensitive; raises
`UnknownGameError` for a blank/unregistered name → the row becomes an `ERROR`).
The roster is `config.GAMES`; **add a game by appending one `Game` to
`config._GAMES`**. Fields: `repo_dir`, `build_dir`, `binary`, `base_branch`,
`build_commands` (cmake configure+build for SuperTux; a game on another build
system supplies its own command list), `cache_artifacts`, `rec_dir`,
`launch_args`, `replay_flag` / `replay_delay_flag` / `replay_delay` /
`replay_margin`, and `seconds_per_step`.

**gamerv catalog selection.** gamerv keeps a per-game catalog bundle in
`config/games/<name>/` (`game_profile.json` + `event_types.json`) and picks the
active one from the `GAME` env var (which overrides its `config/active_game.txt`).
For each row the controller sets `GAME=<Game Name>` for the whole `run_case`
(restored afterwards), so every worker op — `check_formula`, `log`, `validate`,
`compare_bug_condition` — loads that game's predicate catalog. Before touching any
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
   game auto-fire the replay, wait until the **game reports the replay over**
   (it writes the file named by `$GAMERV_REPLAY_DONE` where its replay freezes:
   tape played to its end, or the run's own end), keep logging the frozen last
   frame for `game.replay_margin`, then SIGINT the logger. The end is never
   estimated: frames × `game.seconds_per_step` only sizes the timeout
   (`REPLAY_TIMEOUT_FACTOR` × that + `REPLAY_TIMEOUT_SLACK`), and a replay that
   misses it fails the row (ERROR) instead of leaving a cut-short trace. Traces
   are **not** validated here.
6. Validate every recorded run (`worker.py validate`). If `MANUAL_PREDICATES`
   is on (see below), validate pauses for operator judgement — handled inline.
7. Select the last invalid trace. (A separate fault-localization pass is skipped —
   `compare_bug_condition` ranks the bug conditions itself, so it would duplicate it.)
7a. **Term rules** (`term_rules.py`) — regenerate the game's compound-term
   allow-list, `gamerv/config/games/<game>/fl_term_rules.json`, by digesting the
   game's own source (`runners.source_digest`). Unconstrained, the compound sweep
   *is* the candidate explosion: one Super Tux run mined 2136 perfectly-separating
   candidates, 2127 of them compound and only 9 over a single variable. The rules
   keep the combinations the source actually forms — if the game computes or
   compares `a` against `b`, `a - b` is a quantity the game is about; if it never
   does, it is arithmetic noise.

   It runs **here**, not inside the step-2a window, because the key records carry
   pre-edit line numbers: the tree the digest scans must be the pristine one, and by
   step 7 the instrumentation has been reverted and a run exists to read base fields
   from. Cached on `instrument.fingerprint(game)` — the same content key the build
   cache uses — so a row that changed nothing pays a file read.

   Both inputs are **required**, and the step refuses to write without them: the key
   table kept from step 2a (`instrument.keys_path`, copied aside before the revert
   deletes the manifest) and a recorded run. Each covers half the vocabulary, and the
   rules are a *strict* allow-list, so a combination missing from them is one the
   miner is forbidden to build. Digesting Super Tux with the accessor bridge alone
   permits 26 combinations and blocks three of its own dataset rows, whose ground
   truths name instrumented locals. An absent rules file means allow-all — worse for
   the candidate count, safe for correctness — so that is the state it fails into.
8. Rank the bug conditions and decide `phi` (`worker.py compare_bug_condition`) —
   `phi`'s own Ochiai score against the top tier, then `∃ B ∈ T . phi -> B` (each
   top-ranked candidate checked on its own; see *Bug-condition ranking* below).
9. Emit SUCCESS / FAIL (plus CONDITION_NOT_FOUND / VARIABLE_NOT_FOUND /
   INCONCLUSIVE / ERROR for diagnostics — see *Result statuses* below).

Mining is **generic** — gamerv auto-discovers candidate atoms from
every logged variable. (The old per-row `Target Variables` "box-only" vocabulary
and its `--use-target-variables` flag have been removed.)

#### Step 2a — source instrumentation (local variables)

Mining can only rank conditions over variables the snapshot actually carries, and a
function-local is invisible unless the source reports it. So **before every
compile** the controller runs gamerv's source instrumenter over the checked-out
tree (`runners.source_instrument`, gamerv branch `local_vars`): it parses the game
with libclang, selects the locals worth reporting, and splices the `REPORT_LOCAL`
calls in. The compiler then sees the instrumented source, and **the edit is
reverted as soon as the build finishes** — pass or fail — so the repo is clean for
the next checkout and the instrumentation never lands on a dataset branch.

It lives inside `game_repo.build()` rather than beside it in `run_case`, so that
it wraps *every* compile from a single place. Its cost shows up in the `build`
phase timing.

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

#### Bug-condition ranking (step 8)

Step 8 is spectrum-based fault localization. Every P-state of the row's traces is a
test — failing when it is a violation (`P & ~Q`), passing when it is good
(`P & Q`) — and every candidate condition `B` over the raw logged variables (single
literals, and the pairs that beat the best single one) is ranked by its **Ochiai**
score `ef / sqrt(F·(ef + ep))`: `ef` / `ep` the failing / passing states `B` holds
on, `F` the failing states in all. Neither side is a hard constraint: a candidate
that misses a violation, or holds on a good state, ranks lower rather than being
discarded. The row's `phi` — the condition under which the bug occurs — is scored
the same way and decided against the top tier (the best score, then fewest
literals):

| phi's score | status |
|---|---|
| below the top (*OUTRANKED*), or holds on no violation (*VACUOUS*) | FAIL |
| above the top (*OUTRANKS* every mined candidate) | CONDITION_NOT_FOUND |
| equal, and a single top-ranked `B` over phi's variables has `phi -> B` | SUCCESS |
| equal, no such `B` | CONDITION_NOT_FOUND |

A `phi` that reads a non-raw catalogue predicate has no score of its own and is
decided by containment against the top tier alone. The containment verdict is
persisted to the run dir's `phi_verdict.json` (rank, the candidate that established
`phi`, `union_only`), the whole ranking to `bug_conditions.log`.

Each numeric candidate is placed at its Ochiai-best split and spelled at the
loosest edge of that data gap (`x > -1` when no value lies between -1 and the next
one), so a `phi` splitting in the same gap implies it. A split is one-sided, so a
numeric field or container path also gets `x == v` for up to 8 of its most
frequent values, and the ranking offers `~(x == v)`: that is the only candidate a
negated equality ground truth (`skid_state != 0`, `m_floor_normal.y != 0`) can be
contained in. A value qualifies when it renders exactly at 3 decimals and either
recurs (seen in two or more states) or belongs to a quantity with only two observed
values — there the equality cuts the data exactly where the threshold does, so it
adds a spelling but no new partition to overfit. A one-off value among three or
more would let `~(x == v)` carve a single awkward state out of an imperfect split.
Arithmetic compounds get no equality atoms — their values recur by coincidence.

Three more vocabulary rules serve ground truths written as negations: the compound
budget breaks its (on small pools, massive) ties by the smaller term first and
keeps a term's `Exists`/`Forall` variants together — a negated existential
`~(Exists(t > c))` is contained only in `Forall(t < b)`; an opaque base (`subject`)
gets each split spelled from both edges, so a phi like `subject.meleeDamages <= 0`
can match exactly; and a two-valued field's negation is dropped as redundant only
for booleans, since `~(fsm_state == "run")` is not `fsm_state == "spawn_ship"`. And
before containment, `phi` is expanded into its disjunctive normal form and the
alternatives no recorded state satisfies are dropped — a condition written as the
negation of another hides its alternatives under the negation.

**Phi-shaped candidates.** A bug condition is often an "and" of independent facts,
`A & B`. As a two-literal pair it is dropped whenever it only ties the best single
condition (a pair must beat every one), which on a small pool, where hundreds of
single conditions are perfect, is always. When `phi` has top-level `&` parts, gamerv
also mines `A & B` conjunctions over distinct fields, each entered as *one*
candidate (one atom). The arity comes
from `phi`'s own `&` count, a field is represented by its loosest tied-best literal,
and a conjunction is kept only when it beats every part (and, for three or more
parts, every smaller conjunction inside it). A `phi` with top-level `|` parts gets
the disjunctive twin: `A | B` over distinct quantities, kept only when it beats
every alternative. On tiny pools (one violation) this adds many coincidental
perfect conjunctions to the tier: rows 78 and 107 grow from about 200 to 7k–16k
candidates.

Keeping *both* sides soft is deliberate: with "holds on every violation" as a hard
rule, every coefficient (Ochiai, Tarantula, Jaccard) reduces to a function of the
good states alone.

**The dataset.** `dataset_bug_condition.csv` is the dataset and the file to edit
(`config.DATASET`; `$FL_DATASET` or `$FL_DATA_DIR` override it, and `data/` holds the
published snapshot). Its `Target Phi` reads like the mutation condition
(`player.wading == true`, not `~(player.wading == false)`): it was generated as the
negation of the earlier good-condition dataset, the negation pushed inside (De
Morgan, `Exists` <-> `Forall`, flipped comparison operators, double negations
dropped). Only row 95 keeps a `~`, on `~Exists(a & b)`, because a quantifier body
cannot be a disjunction.

**Archived: the refinement objective.** Until 2026-09-30 step 8 could instead mine
*refinements*, conditions `R` under which the property is sound. Such an `R` is
false on every violation, `G(P & R -> Q)` holds, and `R` is ranked by the good
states it keeps. Those runs were scored against the good-condition `dataset.csv`,
under the `--objective`, `--refinement-mode` and `--threshold-strategy` knobs. All
of it is archived at the `refinement-archive` tag of the controller, gamerv and data
repos (`git checkout refinement-archive`). Its results folders are under
`results/refinement_archive/`, and `dataset.csv` is under `../archive/refinement/`.
Archived runs can still be read. `report.py`, `plots.py` and `compare.py` read the
old `REFINEMENT_NOT_FOUND` status as `CONDITION_NOT_FOUND` and the old `refine`
phase as `localize`, and `metrics.read` falls back to an archived run dir's
`phi_refinement.json`.

#### Candidate ordering (step 8)

A candidate's score is its Ochiai suspiciousness, and the reported candidate set
is the **top tier**: the leading run of candidates sharing the best score *and* the
fewest literals, with the redundant threshold spellings inside it folded away.
Inside that tier every candidate implicates the fault equally strongly, so the
order they are read in is a free choice — and it is what `gt_rank` measures
against. `--candidate-sort` (`config.CANDIDATE_SORT`) selects it:

- `none` (default) — the miner's own order: score, then fewest literals, then
  generation order. The baseline. Note it is not a *neutral* control: generation
  order groups atoms by object and field and puts every positive literal before
  every negated one.
- `type` — by the kind of value the atom compares against: boolean flags
  (`player.on_ground == true`), then parity tests (`player.coins % 2 == 0`),
  then categorical equalities (`player.bonus == "fire"`, a container literal, or
  two quantities compared), then numeric thresholds (`player.vy < 0`). The
  premise is reading cost: a flag states a fact, a threshold makes the reader
  judge a number. A conjunction is ranked by its *worst* literal. Expect this to
  move a lot of rows — roughly three quarters of mined atoms are thresholds.
- `popularity` — by how often the atom's quantities are mentioned in the game's
  own source, most-mentioned first, scoring a compound by its **least**-mentioned
  operand. The counts come from step 7a's digest
  (`gamerv/config/games/<game>/fl_var_popularity.json`); a derived centre
  (`cx`/`cy`) is scored through the raw components it is computed from. With no
  counts for the game gamerv says so on stdout and keeps the mined order.

**Score and size always stay the primary keys**, so a sort only permutes
candidates that are already tied on both: the tier boundaries and the candidate
count mean the same under every sort, the most suspicious condition never moves
(it is ranked before the sort), and two runs are directly comparable.

The tier also ends at the first **longer candidate at the same score**, though
under the Ochiai ranking that never happens in practice: gamerv keeps a pair only
when it beats every single literal, and a phi-shaped conjunction or disjunction is
one atom. What *is* held out of the tier, because something already in it implies
it, is kept in `bug_conditions.log` under its own divider:

- a **tighter or differently-spelled split** of a bound the tier already carries.
  `Forall(e.h < 22)`, `Forall(e.h < 58)` and `~(Exists(e.h > 116))` are three
  spellings of one cap on `max(e.h)`, and the loosest implies the other two. The
  two polarities fold separately and a negated literal may absorb a positive one
  but never the reverse: a positive literal reads False where its quantity is
  missing or non-numeric, its negation True.

Dropping implied candidates cannot change a verdict — the test is
`Exists B: phi -> B`, and the retained candidate is the weaker one, so it accepts
at least as many `phi`s — but it does move `candidates` and `gt_rank`, which is
the point. It can also move `gt_match` from `exact` to `implication` where `phi`
was spelled at a tighter split than the retained candidate: that spelling is not
a candidate the tool would show.

Re-reading an **archived** run whose verdict ranked `phi` over the whole list
rather than the tier reports a rank past the tier as `below_top` (a miss). To
compare orderings, rank every row under several sorts in one run:

```bash
./run_experiment.sh --skip-manual --candidate-sort all          # none, then type, popularity
./run_experiment.sh --skip-manual --candidate-sort type,none    # or name them; the first is the rows' own
```

Each row is **mined once**, under the first sort — its verdict, notes and `gt_rank`
are that sort's — and then **step 8b** re-ranks the cached candidates under each
further sort: one more `compare_bug_condition` call per sort that reuses the
mining cache (the sort is not part of its signature), so gamerv only re-orders the
tier and re-decides `phi`, exactly as a run under that sort would. It costs about a
second per sort per row, timed in its own `rerank` phase so the method's `localize`
time is not inflated by it. The run writes **one** `results/run_<timestamp>_bug_sorts/`
folder:
`gt_rank_none` / `gt_rank_type` / `gt_rank_popularity` columns, a `RANK BY
CANDIDATE SORT` table in `report.txt` (and on stdout: median rank per sort per game,
and how often each sort ranks the ground truth best), and `ranks_by_sort.png`. The
candidate count and the verdict cannot move between sorts; step 8b prints a
warning if either does. Two separate runs can still be diffed row by row with
`src/compare.py A B`.

#### Result statuses (step 9)

Every row ends on exactly one of these:

| status | meaning |
| --- | --- |
| `SUCCESS` | `phi` ties the top Ochiai score and is contained in **a single** top-ranked bug condition `B` (`phi -> B`). |
| `FAIL` | `phi` was decided and rejected: the data ranks another condition above it (*OUTRANKED*), it holds on no violation (*VACUOUS*), or a non-raw `phi` is contained in no top-ranked candidate. |
| `CONDITION_NOT_FOUND` | `phi` scores as high as the best mined candidate, or higher, but no single top-ranked `B` contains it — **the search missed it**, `phi` is not wrong. Includes the rows only the *union* of several candidates covers (`union_only`). Archived runs call it `REFINEMENT_NOT_FOUND`. |
| `VARIABLE_NOT_FOUND` | `phi` could not be evaluated at all: it reads a quantity **the game never exports**, so no logged state carries it. Nothing was decided about `phi`; the gap is in the game's reported vocabulary (see step 2a and gamerv's `report_state` coverage), not in the row. |
| `INCONCLUSIVE` | Nothing to decide `phi` against — no tape recorded, no invalid trace, no positives to contrast, no candidate mined. |
| `SKIPPED` | The row has no `Target Phi`; its bug conditions are still ranked and the most suspicious one reported, but there is no ground truth to compare. |
| `NEEDS_MANUAL` | `--skip-manual` only. Step 6 hit a manual visual/audio predicate the stored hints do not cover, so the row was left for a human instead of stopping the batch at a prompt. Nothing was decided; re-run the row interactively to classify it (see *Manual predicate values*). |
| `ERROR` | The experiment itself broke on this row — unknown game, missing gamerv catalog, formula rejected, build/worker crash. **The only status that makes the run exit non-zero.** |

`NEEDS_MANUAL` is not `ERROR` either: nothing broke, the row just has not been
run yet. An unattended batch is *finished* only once its `NEEDS_MANUAL` rows have
been re-run interactively — the closing summary lists them by row number.

`VARIABLE_NOT_FOUND` and `CONDITION_NOT_FOUND` are deliberately *not* `ERROR`:
both are outcomes of the method under test (an unexported variable, an
incomplete ranking), and a batch of them is a finding to report, not a broken
run.

### Every row is a mutant row (record on the mutant alone)

Step 5 records the row's tapes on the mutant branch only. A mutant that breaks
its property under some condition yields a trace holding both good (`P & Q`) and
violation (`P & ~Q`) states, which is what mining contrasts to decide phi.

A mutation that is **always-on** for the level its property covers — e.g.
`change_music_config` plays the wrong music on *every* entry to its level —
leaves a trace with only violations and no good states to contrast, so mining
returns `no_positives` and the row is reported INCONCLUSIVE.

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
./run_experiment.sh --skip-manual          # unattended: skip rows needing a human
./run_experiment.sh --candidate-sort type   # reorder each score tier: flags before thresholds
./run_experiment.sh --skip-manual --candidate-sort all  # mine once, rank under all three sorts
./run_experiment.sh --row 4 --no-instrument  # skip step 2a (build source as committed)
./run_experiment.sh --row 4 --clean         # drop row 4's cached build + logs, then stop
```

Build output is **condensed to a single self-updating line** per build command
(`  [Super Tux] build  ████████░░░░░░░░  47%  3m04s  object/player.cpp`) — an
inherited compile of a big game prints thousands of lines and scrolls the
pipeline's own step headers off the screen. Nothing is dropped: the full output
of a branch's last build is at `build_logs/<game>/<branch>.log`, compiler
`error:` lines are echoed as they happen, and a failing command that said
nothing error-shaped dumps its tail. Piped to a file (no TTY to redraw), the same
line is printed once per 10% instead. To watch a raw build:

```bash
CONTROLLER_BUILD_OUTPUT=raw ./run_experiment.sh --row 4   # inherit the compiler's stdout
```

Results are written to one folder per run, `results/run_<timestamp>_bug/`
(`_bug_sorts` for a run ranked under several candidate sorts — the bare
`run_<timestamp>` was a refinement run's, see the archive note above), and
summarized on stdout. The closing stdout summary is **aggregate only** — the
classification counts, the mining rates and the per-game / per-category divisions;
every per-case number (verdict, note, mining metrics, phase timings) goes to
`report.txt` and `full.csv`, so a 120-row batch's terminal output stays readable
(the per-row progress printed *while* the run walks the rows is unaffected):

| file | what it holds |
|---|---|
| `full.csv` | every column the pipeline produced (the dataset `row` number, fault type + parent category, phi, formula, notes, mining metrics, per-phase timings) — the archival record. |
| `summary.csv` | the five columns a results table needs: game, branch, property, ground truth, and the bug condition found (the top-ranked candidate the verdict is about, column `B` in `full.csv`). |
| `mining.csv` | step 8's search metrics per row: how many top-ranked candidate bug conditions were mined, the ground truth's rank among them, the match tier, and whether only the union of several candidates covered it. The **rank** is a position in the order `--candidate-sort` chose; the count is the tier's size and is the same under every sort. Also the size of the state the miner searched: `variables` (every `base.field` in the pooled good/violation states) and `varying_variables` (the ones not constant across them — the vocabulary atoms are built over), counted by gamerv (`raw_atoms.state_variables`) and blank for a row that never pooled states. Then the labelled examples the ranking learned from: `good_states` (pooled trigger states where the spec held) and `bad_states` (violations) — `scripts/backfill_states.py <run>` adds both to a run written before them. Then `gt_rank_<sort>` per candidate sort: the run's own sort always, every sort under `--candidate-sort all`. |
| `settings.json` | the knobs the batch ran under — candidate sort(s), instrumentation, row/game selection, dataset (and `objective`, always `bug-condition`, so an archived refinement run is told apart). Two runs over the same rows under different knobs are otherwise indistinguishable; `report.txt` and the figures read it back and caption themselves with it. |
| `report.txt` | the **aggregate** view: rows per classification, then the same counts per game, per **bug category** and per **parent category**, plus the mining rates for each — then `PER-GAME BREAKDOWN`, which repeats all of those cuts **over one game's rows at a time** (that game's classifications, category tables and mining numbers, with no other game in the denominators) — then the **per-case** division (`BY CASE`): one line per dataset row with its verdict, candidate count, ground-truth rank, match tier and note, followed by `BY CASE — TIMINGS` (per-case wall clock and phase seconds, with the run's totals). |
| `success_by_fault_type.png` | one bar per **bug category**, grouped under its parent, split into success / fail / not mined (C_NF) / undecided (V_NF, INCO) / not evaluated (SKIP, MANU, ERR), with the success rate at its end. |
| `success_by_parent.png` | the same split for the two **parent categories** (non-temporal vs temporal). |
| `success_by_game.png` | the same split **per game**, with the run's pooled success rate in the subtitle. |
| `time.png` | **time** of the method's own phases: one panel for validate + localize (over the cases that ran both), one for each alone, a box per game, each panel on its own axis. Build and record are left out — they depend on the game and its data, not on the method; their seconds stay in `full.csv` and `report.txt`. |
| `space.png` | **space**: per game, the `variables` and `varying_variables` counts side by side. |
| `time_vs_space.png` | the localize phase's seconds against the varying-variable count, one panel per game with every other game's rows in gray behind it. |
| `candidates.png` | boxplot of the candidate count, one box per game, every row a point beside its box — over every row that mined, including the ones whose ground truth was not found. |
| `candidates_vs_rank.png` | the same candidate counts **beside** the ground truth's rank, over the rows where it was found — "we mined 128 candidates, and the ground truth was 3rd" in one figure. Both series share one axis (a set size and an index into that set), log-scaled when the spans are far apart. |
| `ranks_by_sort.png` | in place of the above for a run ranked under several sorts (`--candidate-sort all`): per game, one box of candidate counts (the same set under every sort) and one box of ground-truth ranks per sort, over the same rows, each box named with its median. |
| `states_vs_rank.png` | the ground truth's **rank against the labelled states** per row (good + bad), one box per bin, with Spearman's ρ over the unbinned counts — does the truth rank deeper when a row has few examples? Over the rows where the ground truth was found. |
| `states_vs_rank_by_game.png` | the same figure **per game**: the pooled `all` panel, then one panel per game, on the same bins and one shared rank axis, each with its own ρ — does the relation hold inside every game? |

Every artefact after the CSVs is derived *from* them, so each can be regenerated
(or redrawn elsewhere) for any past run:

```bash
python src/report.py results/run_20260930_081748_bug          # rewrite report.txt
python src/report.py results/run_20260930_081748_bug --print  # ...and show it
python src/plots.py  results/run_20260930_081748_bug          # every figure
python src/plots.py  results/run_20260930_081748_bug --figure ranks --out /tmp/r.png
```

### Comparing two runs

A fix is judged by re-running the batch and asking what moved, which is a diff of
two results folders (`src/compare.py`, the **before** run first):

```bash
python src/compare.py results/run_20260910_023507 results/run_20260911_105646
python src/compare.py run_20260910_023507 run_20260911_105646   # bare names resolve under results/
python src/compare.py A B --game "Super Tux"   # only that game's cases
python src/compare.py A B --top 1,5,10,25      # the rank cut-offs to count phi at
python src/compare.py A B --out cmp.txt --csv cmp.csv --all
```

It reads the runs' `full.csv` and splits every changed row the way the question is
asked — the run's own criterion, `SUCCESS` (see *Result statuses*):

```
  gained (now SUCCESS):
    row  29  Super Tux Kart  stunned_animation_sometimes_missing  VARIABLE_NOT_FOUND -> SUCCESS
  lost (no longer SUCCESS):
    row  58  Witch Blast     coin_ui_not_updating                 SUCCESS -> CONDITION_NOT_FOUND
  other changes (neither side SUCCESS):
    row  26  Super Tux Kart  kart_flying_when_tipped              VARIABLE_NOT_FOUND -> FAIL   (closer)
```

then the aggregate deltas (classifications, and the gained/lost split per game,
per parent category and per bug category) and a `MINING` section — the found rate
and the candidate-set size on each side, with the per-case moves, biggest first.

**`GROUND-TRUTH RANK`** is the other half of "did it improve": a fix can leave
every verdict where it stood and still have moved `phi` from rank 40 to rank 2 —
or cut a candidate set from 6873 to 56 — which no status column shows. Over the
cases that mined with a ground truth in **both** runs (the only rows whose two
rank distributions are comparable):

```
  where phi ranked          A      B   delta
  in the top 1              4      4       0
  in the top 3              8      8       0
  in the top 5             12     12       0
  in the top 10            22     22       0
  ranked at all            54     48      -6
  found without a rank      1      1       0
  not found at all         36     42      +6
  ------------------------------------------
  median rank              12     11      -1
  mean reciprocal rank  0.098  0.097  -0.001
  median rank share      4.6%   9.0%  +4.4pt
```

| line | what it says |
|---|---|
| `in the top k` | how often `phi` was among the first *k* mined candidates — the number a results table quotes, since *k* is how far a user reads. The cut-offs are `--top` (default `1,3,5,10`). |
| `ranked at all` | and its two complements: **found without a rank** and **not found**. Only an archived run can have a found-without-a-rank row: that is the retired `region` tier, where `phi` was contained only in a region derived from `phi` itself. It is no longer counted as a find. |
| `median rank` | the middle position, over each side's own ranked rows. |
| `mean reciprocal rank` | `1/rank` averaged over *every* compared case, 0 where `phi` was not ranked — the single number that moves both when a fix finds `phi` on a new row and when it ranks `phi` higher. |
| `median rank share` | the rank as a fraction of that run's candidate set, because the sets move too: rank 11 of 155 is deeper into the list than rank 12 of 236, and only this number says so. |

Then the same cuts per game and per parent category, and the per-case **rank
moves** — ordered by how far `phi` travelled *relative* to where it was (40 -> 2
is the move worth reading, 262 -> 270 is noise), with the ranks gained and lost
at the top:

```
  row  game            branch                                       rank     delta  of candidates                       match  status
   41  Super Tux Kart  stunned_animation_never_finish           - -> 269    ranked     276 -> 333            - -> implication  VARIABLE_NOT_FOUND -> SUCCESS
   13  Super Tux       no_jump_small                          132 -> 153       +21     132 -> 153  implication -> implication  same
```

The rank is a position in the order `--candidate-sort` chose, so a run under
another sort ranks the same candidate set differently — which is exactly what
`SETTINGS` at the top of the comparison is there to catch.

- **A case is its game + branch**, never the dataset row number — that number is a
  position in the dataset and shifts when a row is inserted above it, which
  would silently compare two different mutants. The number is display only (B's,
  falling back to A's), and a case whose number moved is flagged once at the top.
- **Only the cases both runs hold are compared**, so a 120-row batch can be diffed
  against a 24-row one; the rest are listed under `NOT COMPARED` instead of
  counting as a change on either side.
- **The knobs are diffed first** (`SETTINGS`, from each run's `settings.json`): a
  run under another candidate sort or threshold strategy is not the same
  experiment, and is a second explanation for every row below it.
- `closer` / `further` on the *other* changes orders the two statuses by how much
  they settled (`ERROR` < `NEEDS_MANUAL` < `SKIPPED` < `INCONCLUSIVE` <
  `VARIABLE_NOT_FOUND` < `FAIL` < `CONDITION_NOT_FOUND` < `SUCCESS`) — a reading
  aid for skimming, **not** a second success criterion. The verdict line uses
  SUCCESS and nothing else.

Rows that never reached mining have a blank `candidates` cell and are excluded
from the boxplots rather than counted as zero; the rank figure further excludes
the mined rows whose ground truth was never found. Each figure's subtitle says
how many. Plotting needs matplotlib, which lives in the `gamerv` conda env — when
the controller's own env lacks it, `plots.py` re-runs itself in that env, and a
run whose plots cannot be drawn still keeps its CSVs.

### Bug categories

Each dataset row carries a `Fault Type` — one of the benchmark's twelve bug
categories — and the report groups them under two parents:

| parent | categories |
|---|---|
| **Non-temporal fault** | Object out of bounds for any state · Object out of bounds for specific state · Invalid Graphical Representation · Invalid Value Change · Lack of information · Action when not allowed · Action not possible |
| **Temporal fault** | Invalid position over time · Invalid context state over time · Invalid event occurrence over time · Interupted event · Implementation response issues |

The split is the benchmark's own: the first seven are faults of a *single* state
(the snapshot at one event is already wrong), the last five are faults of a state
*sequence* (no single snapshot is wrong; the order or timing across states is).
`dataset.py` owns the lists (`FAULT_TYPES`, `fault_class()`), matches the cell
case- and whitespace-insensitively, and reports an unknown or blank cell under
`(uncategorised)` rather than dropping the row — so a typo in the CSV shows up as
a row to fix instead of leaving a total short. Both category tables are written
to `report.txt` **and** printed under the run's stdout summary, next to the
per-game table (printed whenever a run covers more than one game).

`--clean` deletes the selected rows' `build_cache/<game>/<branch>/` and
`log_cache/<game>/<branch>/` slots and exits **without running anything**, so the
next run of those rows rebuilds and re-records from scratch. It takes the same
`--row`/`--rows`/`--game` selection as a run; unscoped it covers the whole dataset
and asks for confirmation first (`--yes`, or a scope, to skip the prompt — it
refuses outright when stdin is not a terminal). It only ever removes the two
caches, both of which the next run regenerates: `hints/` (operator answers that
cost a human to recreate), `logs_archive/` (the only copy of past runs' traces,
not a cache) and `results/` are left alone.

`--manual-auto` replays previously recorded answers to the manual image/audio
predicate prompts (see *Manual predicate values* below) so a re-run doesn't ask
again.

`--skip-manual` (implies `--manual-auto`) never prompts at all: a row whose manual
predicates the hints do not fully cover is reported `NEEDS_MANUAL` and the batch
carries on with the next row. That is what makes a long run safe to leave
unattended overnight — it cannot stop halfway waiting for a `t`/`f`. Rows the
hints *do* cover run and are decided as usual; the skipped ones are listed by row
number in the closing summary, and re-running just those interactively (with
`--manual-auto`, without `--skip-manual`) both classifies them and caches the
answers, so a second unattended pass covers them.

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
  — **plus a fingerprint of the step-2a instrumenter** (`<tree>+i<fp>`), since its
  edits are what actually get compiled and they never move the tree hash.
  `instrument.fingerprint()` hashes the inputs that decide those edits (the tool's
  own source, the game's spec json, the flags we pass it, and the
  `compile_commands.json` it parses with), so a change to any of them misses the
  cache and rebuilds instead of restoring a binary built with the previous
  instrumentation. It hashes those inputs rather than the edits themselves because
  deriving the edits means parsing the whole game with libclang — the lookup has
  to be cheap, or it costs what the cache saves. The compile database is in there
  because a stale one silently produces *worse* instrumentation rather than
  failing: the same SuperTux tree instrumented 11 files with a leftover database
  and 68 after a fresh cmake configure regenerated it. Turning `--no-instrument`
  on or off flips the key the same way.
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
  detached) a row that needs manual input is reported `ERROR` (`NoTTYError`),
  unless `--skip-manual` was passed, which reports it `NEEDS_MANUAL` and moves on
  (see below). Leave `MANUAL_PREDICATES` empty/unset to keep validation fully
  automatic.
- **Replaying manual answers (`hints/`).** Every answer the operator gives is
  cached under `hints/<game>/<branch>/<recX>.hint` — one file per `.rec`
  recording, keyed by gamerv's stable `"<state_index>::<call>"`. Caching happens
  on *every* interactive run, so the cache builds
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
- `compare_bug_condition` treats non-raw `phi` atoms (e.g. `DiedBySpikes`,
  `~PocketEmpty`) as opaque booleans with no Ochiai score of their own, so `phi -> B`
  typically holds only when `phi` is a raw-variable atom. Aligning `Target Phi` to
  raw atoms is the lever.
