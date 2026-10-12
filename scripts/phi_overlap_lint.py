#!/usr/bin/env python3
"""Fail when a game's instrumentation spec names a variable the ground truth uses.

    python phi_overlap_lint.py [--game "Super Tux"] [--json] [--verbose]

**Why this exists.** ``config/games/<game>/source_instrument.json`` is supposed to
describe the *engine* — its reporting ABI, its type spellings, where its source is
— so that porting the tool to a new game costs a fixed, row-independent amount.
What it actually accumulated is a second thing: entries that publish one specific
variable *because a dataset row refines over it*. Witch Blast's spec states the
practice outright, in its own comment: "Any equipment flag a dataset row refines
over therefore has to be published by name here ... **Add one per slot a row
needs.**"

That is fitting the instrumentation to the answer. A variable no row needs stays
unreported, so the measured result is not "what the tool finds", it is "what the
tool finds after being told where to look". No amount of care in writing the
entries fixes it; the entries are the problem.

The criterion this enforces is not "hand-written vs. generated" but **does the
entry name a program variable, or a property of the engine?** ``report_header``,
``report_state.method`` and ``types.decompose`` name the engine and are fine at
any quantity. ``{"key": "has_leather_boots", "expr": "equip[EQUIP_LEATHER_BOOTS]"}``
names a variable, and it is in the file because row 52 needed it.

So: **no spec entry may name a program variable that appears in a ground-truth
phi.** This script decides that mechanically, by intersecting the phi vocabulary
of the dataset (``config.DATASET``) with the entries of every spec, and it is meant
to run in CI so the property can be *verified* rather than asserted.

WHAT IT CLASSIFIES, and why the three severities are not interchangeable:

  * ``publish`` (**error**) — the entry makes a variable reported, or mineable,
    that would not otherwise be. This is the fitting channel: ``extra_keys``,
    ``key_overrides``, ``policy.include`` pins, ``scale_overrides``,
    ``project_overrides``.
  * ``suppress`` (**error**) — ``policy.exclude`` naming a phi variable. The same
    channel run backwards, and worth the same severity: dropping a key the ground
    truth needs decides a row just as firmly as adding one.
  * ``route`` (**warn**) — ``report_on_routes[].prefer_vars`` names variables, but
    to pick a *host* rather than to publish a value. Wrong host means the whole
    function contributes nothing, so it is not innocent; it is also not per-key.
  * ``protect`` (**info**) — ``external_keys``, ``classifier_keys``,
    ``key.reserved``. These name keys in order to REFUSE them, to stop a member
    silently overwriting geometry another writer owns. Declaring a key here can
    never rescue a row, so it is not a fitting channel and is reported only so the
    overlap is visible.

It also mines the specs' own ``_comment`` blocks for dataset-row citations
("Dataset row 12", "rows 19 and 20"). An entry whose comment cites the row that
motivates it is a **self-declared** fitted entry and is an error regardless of
whether its key happens to appear in the phi vocabulary — which catches the cases
a name intersection cannot, such as ``scan_globs`` gaining a file for one row's
sake.

MIGRATION. The generic mechanism that removes the need for these entries (nested
``report_state`` over every non-scalar member, roots computed from the class
graph) lands over several changes, and until it does the specs cannot be clean. So
findings can be accepted in ``scripts/phi_overlap_allow.json``, each with a
mandatory ``reason``. That file is not an escape hatch, it is the **debt list**:
the script prints its size on every run, and the number going to zero is the
deliverable. A new entry that is not in it fails the build.
"""

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import config                                                   # noqa: E402
import dataset                                                  # noqa: E402


ALLOWLIST = Path(__file__).resolve().parent / "phi_overlap_allow.json"

#: `<object>.<key>` in a Target Phi. The object half is dropped — a spec entry
#: publishes a key onto whatever object hosts it, and which collection that lands
#: in is game_profile's business, not this file's.
_DOTTED = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_.]*)")

#: A trailing unit or axis a spec/emitter appends to the variable's own name:
#: `movement_y` is `movement` decomposed, `last_gain_time_ms` is scaled. Stripped
#: so the phi key still matches the entry that names the underlying variable.
_SUFFIX = re.compile(r"_(x|y|z|w|ms|pct|x10)$")

#: A dataset-row citation in a spec's own prose, BY INDEX — "Dataset row 57",
#: "rows 19 and 20".
_CITES_ROW = re.compile(r"\b(?:dataset\s+)?rows?\s+(\d+(?:\s*(?:,|and|/)\s*\d+)*)",
                        re.I)

#: The same citation BY COUNT — "kart.cpp (4 rows)", "which four rows do mutate",
#: "localplayer.cpp (4 rows, and where the movement logic lives)".
#:
#: This form was invisible to :data:`_CITES_ROW`, which requires the digits to
#: FOLLOW the word, and that is not a cosmetic gap: counting is the register a
#: *selection* argument is written in. Every ``scan_globs`` justification in every
#: spec — the one channel that decides which files are parsed at all, and so which
#: variables can exist — used it, so the check that exists to catch self-declared
#: fitting could not see the place fitting was most likely to be. Found by auditing
#: the specs by hand after the lint reported clean.
_CITES_ROW_COUNT = re.compile(
    r"\b(\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+rows?\b", re.I)


def phi_variables(cases) -> dict[str, dict[str, dict[str, list[int]]]]:
    """``{game: {mode: {variable: [rows]}}}`` — the names ground-truth phis use.

    TWO vocabularies, because a spec entry names its subject in two different
    registers and conflating them produces false failures:

      * ``key`` — what a *published key* is compared against: the phi key as
        written (``on_ground``, ``state_time``), plus the same minus a
        decomposition axis or unit suffix, so the ``floor_normal_x`` half of a
        two-axis entry still matches a phi over ``floor_normal_y``.
      * ``site`` — what a ``file::function::var`` pattern is compared against:
        additionally the tail after a local's ``<scope>__`` prefix, since a
        ``policy.include`` pin spells the variable, not the key
        (``collision_static_constrains__movement_y`` -> ``movement``).

    The tail is deliberately NOT in the ``key`` vocabulary. It was, and it matched
    three entries that had nothing to do with their rows: row 8's
    ``player.set_bonus__type`` (a local of ``set_bonus``) matched ``GameObject``'s
    published ``type``, and row 65's ``player.fire__direction`` matched the
    ``direction`` member of two unrelated entity classes. A published member and a
    local that happens to share a trailing identifier are different variables.
    """
    out: dict[str, dict[str, dict[str, list[int]]]] = {}
    for case in cases:
        if not case.phi:
            continue
        per_game = out.setdefault(case.game, {"key": {}, "site": {}})
        for _obj, path in _DOTTED.findall(case.phi):
            for seg in path.split("."):
                as_key = {seg, _SUFFIX.sub("", seg)}
                tail = seg.rsplit("__", 1)[-1]
                as_site = as_key | {tail, _SUFFIX.sub("", tail)}
                for mode, names in (("key", as_key), ("site", as_site)):
                    for name in names:
                        if not name:
                            continue
                        rows = per_game[mode].setdefault(name, [])
                        if case.row not in rows:
                            rows.append(case.row)
    return out


def _norm(name: str) -> set[str]:
    """The names a spec identifier could be published as.

    A field is named ``m_wading`` and reaches a snapshot as ``wading``
    (``key.member_strip_prefix``), so both spellings have to be tried; the axis
    and unit suffixes come off for the same reason they do in :func:`phi_variables`.
    """
    out = {name}
    for prefix in ("m_", "_"):
        if name.startswith(prefix):
            out.add(name[len(prefix):])
    out |= {_SUFFIX.sub("", n) for n in tuple(out)}
    return {n for n in out if n}


def _site_var(pattern: str) -> str:
    """The variable component of a ``file::function::var`` site pattern."""
    return pattern.split("::")[-1].strip()


def _walk_comments(node, path: str = ""):
    """Yield ``(json path, text)`` for every ``_``-prefixed prose block in a spec."""
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{path}.{key}" if path else key
            if key.startswith("_"):
                text = ("\n".join(value) if isinstance(value, list)
                        else value if isinstance(value, str) else "")
                if text:
                    yield here, text
            else:
                yield from _walk_comments(value, here)
    elif isinstance(node, list):
        for i, value in enumerate(node):
            # Name a list element by its own identity where it has one — a route's
            # `id`, an extra_key's `key` — and fall back to the index only for a
            # genuinely positional list. The path is half of a finding's allowlist
            # fingerprint, so an index would make accepting a finding depend on
            # nothing moving above it in the file: adding one extra_key would
            # silently un-accept every comment below it and fail the build for a
            # reason with no connection to the edit.
            label = i
            if isinstance(value, dict):
                label = value.get("id") or value.get("key") or i
            yield from _walk_comments(value, f"{path}[{label}]")


def _entries(spec: dict):
    """Yield ``(severity_class, match mode, json path, identifier, detail)`` for
    every entry of a spec that names a program variable.

    The mode picks the phi vocabulary to compare against (see
    :func:`phi_variables`): an entry that publishes a KEY is matched against the
    phi keys, one that names a SITE against the variable names inside them.

    Sections not listed here name *types*, *files*, *headers* or *method
    signatures* — the engine, not its variables — and are deliberately not
    checked at all.
    """
    rs = spec.get("report_state") or {}
    policy = spec.get("policy") or {}

    for cls, items in (rs.get("extra_keys") or {}).items():
        for item in items:
            if isinstance(item, dict) and item.get("key"):
                yield ("publish", "key", f"report_state.extra_keys.{cls}",
                       item["key"], item.get("expr", ""))

    for field, key in (rs.get("key_overrides") or {}).items():
        yield ("publish", "key", "report_state.key_overrides", key, field)
        yield ("publish", "site", "report_state.key_overrides",
               field.split("::")[-1], key)

    for pattern in (policy.get("include") or []):
        yield ("publish", "site", "policy.include", _site_var(pattern), pattern)
    for pattern in (policy.get("scale_overrides") or {}):
        yield ("publish", "site", "policy.scale_overrides",
               _site_var(pattern), pattern)
    for pattern in (policy.get("project_overrides") or {}):
        yield ("publish", "site", "policy.project_overrides",
               _site_var(pattern), pattern)

    for pattern in (policy.get("exclude") or []):
        var = _site_var(pattern)
        if var and var != "*":
            yield ("suppress", "site", "policy.exclude", var, pattern)

    for route in (spec.get("report_on_routes") or []):
        for var in (route.get("prefer_vars") or []):
            yield ("route", "site",
                   f"report_on_routes[{route.get('id', '?')}].prefer_vars",
                   var, route.get("target_expr", ""))

    for key in (rs.get("external_keys") or []):
        yield ("protect", "key", "report_state.external_keys", key, "")
    for key in (rs.get("classifier_keys") or []):
        yield ("protect", "key", "report_state.classifier_keys", key, "")
    for key in ((spec.get("key") or {}).get("reserved") or []):
        yield ("protect", "key", "key.reserved", key, "")


#: Which classes fail the build. `protect` never can — declaring a key there
#: refuses it, so it cannot rescue a row.
_SEVERITY = {"publish": "error", "suppress": "error",
             "cited": "error", "route": "warn", "protect": "info"}


def lint_game(game_name: str, spec_path: Path, phi: dict) -> list[dict]:
    if not spec_path.is_file():
        return []
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    vocab = phi.get(game_name, {})
    findings = []

    for cls, mode, jpath, name, detail in _entries(spec):
        names = vocab.get(mode, {})
        hit = next((n for n in _norm(name) if n in names), None)
        if hit is None:
            continue
        findings.append({
            "game": game_name, "class": cls, "severity": _SEVERITY[cls],
            "path": jpath, "name": name, "phi_key": hit,
            "rows": sorted(names[hit]), "detail": detail,
        })

    for jpath, text in _walk_comments(spec):
        cited = _CITES_ROW.search(text)
        rows = (sorted({int(n) for n in re.findall(r"\d+", cited.group(1))})
                if cited else [])
        if not cited:
            # A count citation names no row index, so `rows` stays empty — the
            # prose says "this many rows motivated it" without saying which.
            cited = _CITES_ROW_COUNT.search(text)
        if not cited:
            continue
        findings.append({
            "game": game_name, "class": "cited", "severity": _SEVERITY["cited"],
            "path": jpath, "name": "", "phi_key": "",
            "rows": rows, "detail": " ".join(cited.group(0).split()),
        })
    return findings


def _fingerprint(f: dict) -> str:
    """Identity of a finding in the allowlist. Deliberately excludes ``rows``: a
    row renumbering must not silently re-accept a finding, and adding a row that
    refines over the same key must not raise a second one."""
    return f"{f['game']}|{f['path']}|{f['class']}|{f['name']}"


def load_allowlist() -> dict[str, str]:
    if not ALLOWLIST.is_file():
        return {}
    raw = json.loads(ALLOWLIST.read_text(encoding="utf-8"))
    out = {}
    for item in raw.get("accepted", []):
        reason = (item.get("reason") or "").strip()
        if not reason:
            raise SystemExit(f"{ALLOWLIST}: every accepted finding needs a "
                             f"'reason' — {item}")
        out[f"{item['game']}|{item['path']}|{item['class']}|{item['name']}"] = reason
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--game", action="append",
                    help="limit to one game (repeatable); default is all")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--verbose", action="store_true",
                    help="also print info-severity and accepted findings")
    ap.add_argument("--dataset", type=Path, default=None)
    args = ap.parse_args()

    phi = phi_variables(dataset.read_cases(args.dataset))
    allow = load_allowlist()
    games = args.game or list(config.GAMES)

    findings = []
    for name in games:
        game = config.GAMES.get(name)
        if game is None:
            print(f"unknown game {name!r}; known: {sorted(config.GAMES)}",
                  file=sys.stderr)
            return 2
        spec_path = (config.GAMERV_DIR / "config" / "games" / name /
                     "source_instrument.json")
        findings.extend(lint_game(name, spec_path, phi))

    for f in findings:
        f["accepted"] = _fingerprint(f) in allow
        f["reason"] = allow.get(_fingerprint(f), "")

    if args.json:
        print(json.dumps({"findings": findings,
                          "allowlisted": len(allow)}, indent=2))
    else:
        _report(findings, phi, games, len(allow), args.verbose)

    blocking = [f for f in findings
                if f["severity"] == "error" and not f["accepted"]]
    return 1 if blocking else 0


def _report(findings, phi, games, allowlisted, verbose) -> None:
    total_phi = sum(len(phi.get(g, {}).get("key", {})) for g in games)
    print(f"phi vocabulary: {total_phi} distinct names over "
          f"{len(games)} game(s)\n")

    order = {"error": 0, "warn": 1, "info": 2}
    by_game: dict[str, list] = {}
    for f in findings:
        by_game.setdefault(f["game"], []).append(f)

    shown = 0
    for game in games:
        items = sorted(by_game.get(game, []),
                       key=lambda f: (order[f["severity"]], f["path"], f["name"]))
        visible = [f for f in items
                   if verbose or (f["severity"] != "info" and not f["accepted"])]
        if not visible:
            continue
        print(f"### {game}")
        for f in visible:
            mark = "ACCEPTED " if f["accepted"] else ""
            rows = ",".join(str(r) for r in f["rows"])
            head = f"  {mark}{f['severity']:5} {f['class']:8} {f['path']}"
            if f["name"]:
                head += f" -> {f['name']!r}"
            print(head)
            print(f"        rows {rows}"
                  + (f" | phi key {f['phi_key']!r}" if f["phi_key"] else "")
                  + (f" | {f['detail'][:70]}" if f["detail"] else ""))
            if f["reason"]:
                print(f"        accepted: {f['reason']}")
            shown += 1
        print()

    errors = [f for f in findings if f["severity"] == "error"]
    blocking = [f for f in errors if not f["accepted"]]
    print(f"{len(findings)} finding(s): {len(errors)} error, "
          f"{sum(1 for f in findings if f['severity'] == 'warn')} warn, "
          f"{sum(1 for f in findings if f['severity'] == 'info')} info")
    print(f"accepted debt: {len(errors) - len(blocking)} of {len(errors)} error(s) "
          f"({allowlisted} allowlist entr(y/ies)) — this number reaching 0 is the "
          f"deliverable")
    if blocking:
        print(f"\nFAIL: {len(blocking)} unaccepted error(s). Either remove the "
              f"entry, or add it to {ALLOWLIST.name} with a reason.")
    elif not shown:
        print("\nOK: no spec entry names a ground-truth phi variable.")
    else:
        print("\nOK: every error is accepted debt.")


if __name__ == "__main__":
    sys.exit(main())
