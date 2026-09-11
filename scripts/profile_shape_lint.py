#!/usr/bin/env python3
"""Check each game's object taxonomy (game_profile.json) against its own traces.

    python scripts/profile_shape_lint.py [--game "Witch Blast"] [--by-branch]
                                         [--json] [--verbose]

**Why this exists.** ``config/games/<game>/game_profile.json`` assigns every
object in a snapshot one of three roles, and the role decides the *grammar* a phi
over it must use: a **scalar** base is read directly (``player.energy > 3``), a
**collection** must be quantified (``Exists(enemy.dir == "right")``), and an
object matching neither is mined not at all. The schema has room for exactly two
scalars — the primary and one uid-pointer subject (``GameProfile.from_dict``:
"the model supports one subject role") — so every other distinguished object has
had to be declared a collection whether or not it is one. Witch Blast's and Super
Tux's ``game_session`` are the cases that prompted this: one object, one uid,
every state, quantified anyway.

The role cannot be *inferred* per run — cardinality is a property of the tape, not
of the game (Super Tux Kart's second ``kart`` is in 5 of 60 recordings), and
inferring it would move the atom grammar between runs, so a fixed Target Phi cell
would parse in one run and be a syntax error in the next. So this is a **lint**:
it measures the shape the traces actually show, prints it next to the role the
profile declares, and names the disagreements. Promoting a base stays a reviewed
edit to the profile.

MEASURED PER BUCKET, NOT PER CLASS. The unit is what the profile actually mines —
the primary, the subject, each collection, and the unattributed remainder — not
the raw ``class`` value, because one class can split across buckets: Super Tux's
``hud`` collection matches by ``has_any_key: [game_time_ms]`` and so holds
*exactly one* of the ~60 ``game-object``s in a state while the rest are
unattributed. A class-keyed tally cannot express that and would mis-read the
cardinality of both halves. The unattributed remainder is then broken down by
class, because that is where a promotable singleton would be hiding.

THE SHAPES, and why only the first may be a scalar:

  * ``scalar``     — exactly one, in **every** state. The only shape a scalar base
                     may have: a scalar read resolves to ``{}`` when its object is
                     missing, and every atom over ``{}`` reads False *silently* —
                     the vacuity the ``VARIABLE_NOT_FOUND`` check exists to catch,
                     made uncatchable because the field is in the vocabulary and
                     merely not in this state.
  * ``optional``   — at most one, but absent from some states. Must stay
                     quantified: ``Exists`` is honestly false where it is missing,
                     and ``_unobserved`` drops empty states from the observability
                     pool instead of calling the quantity unexported.
  * ``collection`` — two or more in some state. Cannot be a scalar at all.

The shape is decided by **cardinality alone**. uid churn is measured and printed
(``uid/tr``) but never downgrades a shape: a scalar base is resolved per state —
by its ``uid_field`` where it has one — so Tux keeping his role across a respawn
that gives him a new uid is still one stable base. Churn appears as a caveat on a
promotion, for the reviewer to check, not as a verdict.

Truncated snapshots are excluded from the presence tally, not counted as absence:
FreeMiner drops objects on a byte budget (``truncated: true``) and that is the
writer running out of room, not the game lacking the object.

WHOLE-TRACE ABSENCE IS TWO DIFFERENT THINGS, and they look identical in a
per-state tally: a key the snapshot gained partway through the corpus (every older
recording lacks it in every state) and an object only some levels or branches have
(Super Tux's ``particles-clouds``). They are separated by date — recordings are
stamped ``<branch>/<YYYYmmdd_HHMMSS>`` — so the absence is read as a stale cache
only when every trace holding the bucket is newer than every trace without it, and
as level-dependent otherwise.

SEVERITIES:

  * ``absent`` (**error**) — a scalar base is missing from untruncated states;
    every atom over it reads False there, silently.
  * ``ambiguous`` (**error**) — more than one object *matches* the primary (or the
    declared subject) spec in some state. ``_find_by_spec`` returns the first
    match, so which object the phi talks about is decided by snapshot order.
  * ``promote`` (**warn**) — scalar-shaped, but declared a collection. Every atom
    over it is quantified for nothing, and both quantifiers are generated, so each
    of its fields costs two candidates that cannot disagree.
  * ``invisible`` (**warn**) — scalar-shaped and matched by NOTHING in the
    profile: a stable singleton no atom can reach.
  * ``promote-stale`` / ``invisible-stale`` (**warn**) — the same two, where the
    only absence is chronological. The measurement is then about the cache, not
    the game: re-record the older traces and it becomes a plain promote.
  * ``conditional`` (info) — whole-trace absence interleaved in time: a level- or
    branch-dependent object, which must stay quantified.
  * ``quantified-ok`` (info) — at most one, genuinely absent from some states of a
    trace, declared a collection. Correct as it stands; printed so a reviewer can
    see it was measured and not merely overlooked.
  * ``unmined`` (info) — optional/collection-shaped and unattributed. Expected for
    scenery; listed so the size of the unmined half is visible.

``--by-branch`` adds the per-pool view mining actually sees: within one branch's
states, a collection holding exactly one item in *every* state has ``Forall(x)``
== ``Exists(x)``, so each of its fields contributes two candidates that can never
disagree. That is a per-row property — a pool with an empty state makes ``Forall``
vacuously true where ``Exists`` is false — which is why it is not a profile-level
verdict, and why a collection can show up here (``enemy``, in a branch whose tape
only ever meets one) without being promotable.
"""

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import config                                                    # noqa: E402

sys.path.insert(0, str(config.GAMERV_SRC_DIR))
from predicates_receiver.game_profile import (                    # noqa: E402
    GameProfile, load_file, objects)

#: Shapes, worst-to-best, for sorting a table.
_SHAPES = ("collection", "optional", "scalar")
_UNATTRIBUTED = "unattributed"


class Shape:
    """The measured cardinality of one bucket (or one unattributed class)."""

    def __init__(self, name: str, kind: str):
        self.name = name            # "scalar:player" / "collection:enemy" / a class
        self.kind = kind            # "scalar" | "collection" | "unattributed"
        self.states_present = 0     # states holding >= 1
        self.states_absent = 0      # untruncated states holding none
        self.states_skipped = 0     # truncated states holding none
        self.max_count = 0
        self.multi_states = 0       # states holding >= 2
        self.traces = 0             # traces holding it in at least one state
        self.traces_always = 0
        self.traces_never = 0
        self.traces_partial = 0
        self.absent_traces: list = []
        self.present_stamps: list = []
        self.absent_stamps: list = []
        self.max_uids_in_trace = 0
        self.uids = set()
        self.fields = set()         # distinct keys seen on its objects

    @property
    def shape(self) -> str:
        """Strictly cardinality: only *always exactly one* may be a scalar."""
        if self.max_count >= 2:
            return "collection"
        if self.states_absent:
            return "optional"
        return "scalar"

    @property
    def stale_only(self) -> bool:
        """True when the only absence is WHOLE traces that never hold it at all."""
        return (self.shape == "optional" and self.traces_partial == 0
                and self.traces_never > 0 and self.traces_always > 0)

    @property
    def arrived_at(self) -> str:
        """When whole-trace absence is CHRONOLOGICAL, the stamp it starts at."""
        if not (self.stale_only and self.present_stamps and self.absent_stamps):
            return ""
        first, last_absent = min(self.present_stamps), max(self.absent_stamps)
        return first if first > last_absent else ""

    @property
    def churn_note(self) -> str:
        if self.max_uids_in_trace <= 1:
            return ""
        return (f" (identity churns: up to {self.max_uids_in_trace} distinct uids "
                f"within one trace — check the role is carried by a uid_field "
                f"before promoting)")

    def row(self) -> dict:
        return {
            "bucket": self.name, "kind": self.kind, "shape": self.shape,
            "present": self.states_present, "absent": self.states_absent,
            "truncated_skipped": self.states_skipped,
            "max_per_state": self.max_count, "multi_states": self.multi_states,
            "traces": self.traces, "traces_always": self.traces_always,
            "traces_never": self.traces_never, "traces_partial": self.traces_partial,
            "stale_only": self.stale_only, "arrived_at": self.arrived_at,
            "absent_traces": list(self.absent_traces[:6]),
            "uids": len(self.uids), "max_uids_in_trace": self.max_uids_in_trace,
            "fields": len(self.fields),
        }


def _traces(log_cache: Path, game: str) -> list[Path]:
    return sorted(log_cache.glob(f"{game}/*/*/events.jsonl"))


def _states(path: Path) -> tuple[list[dict], int]:
    """The parsed states of one trace, and the number of unparseable lines."""
    out, bad = [], 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            state = json.loads(line)
        except json.JSONDecodeError:
            bad += 1
            continue
        if isinstance(state, dict):
            out.append(state)
    return out, bad


def _class_of(o) -> str:
    """An object's class, or a stand-in so a classless object is still counted."""
    if not isinstance(o, dict):
        return ""
    cls = o.get("class")
    return str(cls) if cls not in (None, "") else "<no class>"


def _buckets(state: dict, profile: GameProfile) -> dict[str, list]:
    """Every object of one state, grouped the way the profile mines them.

    The primary is resolved by the profile (uid pointer first, then its matcher)
    and removed before the collections classify the rest, exactly as
    ``GameProfile.collection`` does — so the buckets partition the snapshot and
    their counts are the cardinalities the atom grammar sees. The subject is
    recorded as an additional, overlapping bucket: it is a *pointer* to an object
    that usually also belongs to a collection, so counting it exclusively would
    make both halves wrong.
    """
    out: dict[str, list] = defaultdict(list)
    primary = profile.find_primary(state)
    if primary is not None:
        out[f"scalar:{profile.primary.name}"].append(primary)
    subject = profile.find_subject(state)
    if subject is not None:
        out[f"scalar:{profile.subject.name}"].append(subject)
    for o in objects(state):
        if not isinstance(o, dict) or o is primary:
            continue
        if primary is not None and o.get("uid") == primary.get("uid"):
            continue
        owner = profile._owning_collection(o)
        out[f"collection:{owner}" if owner else _UNATTRIBUTED].append(o)
    return out


def measure(traces: list[Path], profile: GameProfile) -> dict:
    """Per-bucket cardinality over every state, plus the unattributed breakdown.

    Counted per TRACE and rolled up afterwards, because absence is only evidence
    once the whole corpus is known: a bucket first seen in the last trace is
    absent from the earlier ones too, and whether those traces lack it *entirely*
    (a vocabulary difference between the binaries they were recorded from) or only
    in some of their states (a genuinely optional object) is the distinction
    :attr:`Shape.stale_only` turns on.
    """
    kinds = {f"scalar:{profile.primary.name}": "scalar", _UNATTRIBUTED: "unattributed"}
    if profile.subject is not None:
        kinds[f"scalar:{profile.subject.name}"] = "scalar"
    for spec in profile.collections:
        kinds[f"collection:{spec.name}"] = "collection"

    shapes: dict[str, Shape] = {}
    unattr: dict[str, Shape] = {}      # the unattributed remainder, by class
    per_trace: list[dict] = []
    n_states = n_truncated = n_bad = 0
    primary_multi = subject_multi = 0

    def bump(store, key, kind, objs, truncated, rec, uids_here):
        sh = store.setdefault(key, Shape(key, kind))
        sh.states_present += 1
        sh.max_count = max(sh.max_count, len(objs))
        sh.multi_states += (len(objs) >= 2)
        for o in objs:
            sh.uids.add(o.get("uid"))
            uids_here[key].add(o.get("uid"))
            sh.fields.update(o)
        rec["present"][key] += 1
        if truncated:
            rec["present_trunc"][key] += 1

    for trace in traces:
        states, bad = _states(trace)
        n_bad += bad
        rec = {"name": f"{trace.parent.parent.name}/{trace.parent.name}",
               "stamp": trace.parent.name, "states": 0, "truncated": 0,
               "present": Counter(), "present_trunc": Counter(),
               "present_cls": Counter(), "present_cls_trunc": Counter()}
        uids_here: dict = defaultdict(set)
        uids_cls: dict = defaultdict(set)

        for state in states:
            n_states += 1
            rec["states"] += 1
            truncated = bool(state.get("truncated"))
            n_truncated += truncated
            rec["truncated"] += truncated

            groups = _buckets(state, profile)
            for key, objs in groups.items():
                bump(shapes, key, kinds.get(key, "collection"), objs,
                     truncated, rec, uids_here)
            # The unattributed half, by class: that is where a promotable
            # singleton would be hiding (Super Tux's camera / music).
            by_cls: dict[str, list] = defaultdict(list)
            for o in groups.get(_UNATTRIBUTED, ()):
                by_cls[_class_of(o)].append(o)
            for cls, objs in by_cls.items():
                sh = unattr.setdefault(cls, Shape(cls, "unattributed"))
                sh.states_present += 1
                sh.max_count = max(sh.max_count, len(objs))
                sh.multi_states += (len(objs) >= 2)
                for o in objs:
                    sh.uids.add(o.get("uid"))
                    uids_cls[cls].add(o.get("uid"))
                    sh.fields.update(o)
                rec["present_cls"][cls] += 1
                if truncated:
                    rec["present_cls_trunc"][cls] += 1

            # A scalar spec that matches more than one object: `_find_by_spec`
            # takes the first, so snapshot order would decide the phi's subject.
            for spec, which in ((profile.primary, "primary"),
                                (profile.subject, "subject")):
                if spec is None or spec.match is None:
                    continue
                hits = sum(1 for o in objects(state)
                           if isinstance(o, dict) and spec.match.matches(o))
                if hits > 1:
                    if which == "primary":
                        primary_multi += 1
                    else:
                        subject_multi += 1

        for store, seen in ((shapes, uids_here), (unattr, uids_cls)):
            for key, uids in seen.items():
                store[key].traces += 1
                store[key].max_uids_in_trace = max(store[key].max_uids_in_trace,
                                                   len(uids))
        per_trace.append(rec)

    for store, pkey, tkey in ((shapes, "present", "present_trunc"),
                              (unattr, "present_cls", "present_cls_trunc")):
        for key, sh in store.items():
            for rec in per_trace:
                if not rec["states"]:
                    continue
                present = rec[pkey].get(key, 0)
                if present == rec["states"]:
                    sh.traces_always += 1
                    sh.present_stamps.append(rec["stamp"])
                elif present == 0:
                    sh.traces_never += 1
                    sh.absent_traces.append(rec["name"])
                    sh.absent_stamps.append(rec["stamp"])
                else:
                    sh.traces_partial += 1
                    sh.present_stamps.append(rec["stamp"])
                untruncated = rec["states"] - rec["truncated"]
                present_untrunc = present - rec[tkey].get(key, 0)
                sh.states_absent += max(0, untruncated - present_untrunc)
                sh.states_skipped += max(0, rec["truncated"] - rec[tkey].get(key, 0))

    return {"shapes": shapes, "unattributed": unattr, "states": n_states,
            "truncated": n_truncated, "bad_lines": n_bad, "traces": len(traces),
            "primary_multi": primary_multi, "subject_multi": subject_multi}


def _stale_note(sh: Shape) -> str:
    return (f"present in every state of {sh.traces_always} trace(s) and none of "
            f"{sh.traces_never}, and every trace that holds it is newer than every "
            f"trace that does not (first at {sh.arrived_at}) — the export landed "
            f"mid-corpus, so this measures the cache, not the game")


def _findings(m: dict, profile: GameProfile) -> list[dict]:
    out = []
    for key, sh in m["shapes"].items():
        row = sh.row()
        if sh.kind == "scalar":
            if sh.shape == "collection":
                out.append({**row, "severity": "error", "verdict": "ambiguous",
                            "note": f"resolves to up to {sh.max_count} objects "
                                    f"({sh.multi_states} state(s)) — the scalar "
                                    f"read takes the first"})
            elif sh.shape == "optional" and key != f"scalar:{profile.primary.name}":
                # The subject is a per-event pointer; having none is normal.
                continue
            elif sh.shape == "optional":
                out.append({**row, "severity": "error", "verdict": "absent",
                            "note": f"the primary is missing from "
                                    f"{sh.states_absent} untruncated state(s) — "
                                    f"every atom over it reads False there, "
                                    f"silently"})
            continue
        if sh.shape == "scalar":
            out.append({**row, "severity": "warn", "verdict": "promote",
                        "note": "exactly one in every state — quantified for "
                                "nothing, and both quantifiers are generated over "
                                f"each of its {len(sh.fields)} fields"
                                + sh.churn_note})
        elif sh.stale_only and sh.arrived_at:
            sample = ", ".join(sh.absent_traces[:3])
            out.append({**row, "severity": "warn", "verdict": "promote-stale",
                        "note": f"scalar-shaped wherever it is reported: "
                                f"{_stale_note(sh)}. Re-record ({sample}, ...) and "
                                f"it becomes a promote."})
        elif sh.stale_only:
            out.append({**row, "severity": "info", "verdict": "conditional",
                        "note": f"whole-trace absence interleaved in time "
                                f"({sh.traces_always} trace(s) hold it, "
                                f"{sh.traces_never} never do) — level- or "
                                f"branch-dependent; must stay quantified"})
        elif sh.shape == "optional":
            out.append({**row, "severity": "info", "verdict": "quantified-ok",
                        "note": f"optional: correct as a collection "
                                f"({sh.states_absent} state(s) hold none)"})

    for cls, sh in m["unattributed"].items():
        row = sh.row()
        if sh.shape == "scalar":
            out.append({**row, "severity": "warn", "verdict": "invisible",
                        "note": f"singleton in every state, matched by no base — "
                                f"no atom can reach its {len(sh.fields)} fields"
                                + sh.churn_note})
        elif sh.stale_only and sh.arrived_at:
            out.append({**row, "severity": "warn", "verdict": "invisible-stale",
                        "note": f"matched by no base, and {_stale_note(sh)}"})
        else:
            out.append({**row, "severity": "info", "verdict": "unmined",
                        "note": f"{sh.shape}-shaped and matched by no base"})
    return out


def lint_game(game: str, log_cache: Path) -> dict:
    path = config.GAMERV_DIR / "config" / "games" / game / "game_profile.json"
    if not path.is_file():
        return {"game": game, "error": f"no game_profile.json at {path}"}
    profile = load_file(path)
    traces = _traces(log_cache, game)
    if not traces:
        return {"game": game, "error": f"no cached traces under {log_cache / game}"}

    m = measure(traces, profile)
    return {"game": game, "profile": str(path), "traces": m["traces"],
            "states": m["states"], "truncated": m["truncated"],
            "bad_lines": m["bad_lines"],
            "buckets": [sh.row() for sh in m["shapes"].values()],
            "unattributed_classes": [sh.row() for sh in m["unattributed"].values()],
            "findings": _findings(m, profile)}


def by_branch(game: str, log_cache: Path) -> list[dict]:
    """Per-branch pools whose collections hold exactly one item in every state.

    The duplicate-quantifier view mining sees for one row: with one item in every
    state of the pool, ``Forall(x)`` and ``Exists(x)`` are the same predicate, so
    each of that collection's fields contributes two candidates that can never
    disagree. Conditioned on the pool, because an empty state makes ``Forall``
    vacuously true where ``Exists`` is false.
    """
    profile = load_file(config.GAMERV_DIR / "config" / "games" / game /
                        "game_profile.json")
    out = []
    for branch_dir in sorted((log_cache / game).glob("*")):
        if not branch_dir.is_dir():
            continue
        counts: dict[str, list] = defaultdict(list)
        fields: dict[str, set] = defaultdict(set)
        n = 0
        for trace in sorted(branch_dir.glob("*/events.jsonl")):
            states, _ = _states(trace)
            for state in states:
                n += 1
                groups = _buckets(state, profile)
                for spec in profile.collections:
                    objs = groups.get(f"collection:{spec.name}", ())
                    counts[spec.name].append(len(objs))
                    for o in objs:
                        fields[spec.name].update(o)
        if not n:
            continue
        singles = sorted(name for name, xs in counts.items()
                         if xs and all(x == 1 for x in xs))
        if singles:
            out.append({"branch": branch_dir.name, "states": n,
                        "always_one": singles,
                        "fields": {name: len(fields[name]) for name in singles}})
    return out


def _table(title: str, rows: list[dict], label: str) -> None:
    if not rows:
        return
    print(f"\n  {title}")
    print(f"  {label:26} {'shape':10} {'present':>8} {'absent':>7} {'max':>4} "
          f"{'fields':>6} {'uid/tr':>6} {'traces a/n/p':>13}")
    for row in sorted(rows, key=lambda r: (_SHAPES.index(r["shape"]),
                                           -r["present"], r["bucket"])):
        traces = f"{row['traces_always']}/{row['traces_never']}/{row['traces_partial']}"
        print(f"  {row['bucket'][:26]:26} {row['shape']:10} {row['present']:>8} "
              f"{row['absent']:>7} {row['max_per_state']:>4} {row['fields']:>6} "
              f"{row['max_uids_in_trace']:>6} {traces:>13}"
              + ("  [stale-only absence]" if row["stale_only"] else ""))


def _report(results: list[dict], branch_reports: dict, verbose: bool) -> None:
    order = {"error": 0, "warn": 1, "info": 2}
    for res in results:
        print(f"### {res['game']}")
        if res.get("error"):
            print(f"  skipped: {res['error']}\n")
            continue
        print(f"  {res['traces']} trace(s), {res['states']} state(s)"
              + (f", {res['truncated']} truncated" if res["truncated"] else "")
              + (f", {res['bad_lines']} unparseable line(s)"
                 if res["bad_lines"] else ""))
        if verbose:
            _table("declared bases:", res["buckets"], "bucket")
            _table("unattributed, by class:", res["unattributed_classes"], "class")
        findings = sorted(res["findings"],
                          key=lambda f: (order[f["severity"]], f["bucket"]))
        visible = [f for f in findings if verbose or f["severity"] != "info"]
        print()
        for f in visible:
            print(f"  {f['severity']:5} {f['verdict']:15} {f['bucket'][:26]:26} "
                  f"[{f['kind']}]")
            print(f"        {f['note']}")
        if not visible:
            print("  nothing to report (use --verbose for the full tables)")
        if res["game"] in branch_reports:
            rs = branch_reports[res["game"]]
            print(f"\n  per-branch pools with an always-one collection "
                  f"({len(rs)} branch(es)):")
            for r in rs:
                names = ", ".join(f"{n} ({r['fields'][n]} fields)"
                                  for n in r["always_one"])
                print(f"    {r['branch'][:38]:38} {r['states']:>5} state(s)  "
                      f"Forall == Exists for: {names}")
        print()

    counts = Counter(f["severity"] for res in results for f in res.get("findings", ()))
    print(f"summary: {counts.get('error', 0)} error, {counts.get('warn', 0)} warn, "
          f"{counts.get('info', 0)} info over {len(results)} game(s)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--game", action="append",
                    help="limit to one game (repeatable); default is all")
    ap.add_argument("--log-cache", type=Path, default=config.LOG_CACHE_DIR,
                    help="trace root: <log-cache>/<game>/<branch>/<ts>/events.jsonl")
    ap.add_argument("--by-branch", action="store_true",
                    help="also report the per-pool duplicate-quantifier view")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--verbose", action="store_true",
                    help="also print the full tables and info findings")
    args = ap.parse_args()

    games = args.game or list(config.GAMES)
    unknown = [g for g in games if g not in config.GAMES]
    if unknown:
        print(f"unknown game(s) {unknown}; known: {sorted(config.GAMES)}",
              file=sys.stderr)
        return 2

    results = [lint_game(g, args.log_cache) for g in games]
    ok = {r["game"] for r in results if not r.get("error")}
    branch_reports = ({g: by_branch(g, args.log_cache) for g in games if g in ok}
                      if args.by_branch else {})

    if args.json:
        print(json.dumps({"games": results, "by_branch": branch_reports},
                         indent=2, default=str))
    else:
        _report(results, branch_reports, args.verbose)

    errors = sum(1 for res in results for f in res.get("findings", ())
                 if f["severity"] == "error")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
