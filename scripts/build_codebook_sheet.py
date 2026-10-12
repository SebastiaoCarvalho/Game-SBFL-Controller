#!/usr/bin/env python3
"""Build the timing-failure codebook's rating sheet (inputs only, answers blank).

    python scripts/build_codebook_sheet.py results/codebook/rating_sheet.csv

One line per rated row. The inputs are looked up, not judged:
  decision_site  every line of the mutant branch holding the row's anchor - the text of
                 the dataset's Mutation Condition as the branch writes it (ANCHORS below)
  record_sites   every line of the mutant branch using P's event constant
                 (EVENT_<NAME> natively, GameRV.EV.<NAME> in Asteroids); FreeMiner's
                 bus-dispatched events also list the MtEvent's put sites
  phi_keys       each snapshot key the Target Phi reads, with its kind (member, local,
                 subject, collection, session), from the key's shape
More than one hit is listed, never resolved: the rater picks and says so.
"""

import csv
import json
import re
import subprocess
import sys
from pathlib import Path

DATASET = Path("/home/sebastiao/nii/fl/dataset_bug_condition.csv")
EVENT_TYPES = Path("/home/sebastiao/nii/fl/gamerv/config/games")
REPOS = {"Super Tux": "/home/sebastiao/supertux", "Super Tux Kart": "/home/sebastiao/stk/stk-code",
         "Witch Blast": "/home/sebastiao/witchblast", "Asteroids": "/home/sebastiao/asteroids",
         "FreeMiner": "/home/sebastiao/freeminer"}
# FreeMiner events the logger records from the event bus (event_log_server.cpp BUS_EVENTS).
FM_BUS = {"damage": ("PLAYER_DAMAGE", "MtEvent::PLAYER_DAMAGE"),
          "dig_node": ("NODE_DUG", "NodeDugEvent("),
          "punch_swing": ("CAMERA_PUNCH_LEFT", "MtEvent::CAMERA_PUNCH_LEFT")}
# Where each game defines its event ids: name = id (native enum) or NAME: id (JS table).
EVENT_DEFS = {"Super Tux": "src/predicates/event_log_server.hpp",
              "Super Tux Kart": "src/predicates/event_log_server.hpp",
              "Witch Blast": "src/predicates/event_log_server.hpp",
              "Asteroids": "predicates/event_log.js",
              "FreeMiner": "src/client/predicates/event_log_server.h"}

# Row -> (file, text of the mutation condition as the mutant branch writes it).
ANCHORS = {
    15: ("src/object/player.cpp", "const bool striding = on_ground() && is_big()"),
    17: ("src/badguy/badguy.cpp", "if (squisher && squisher->get_bbox().overlaps(m_col.m_bbox))"),
    18: ("src/object/player.cpp", "if (!completely && m_post_damage_safety_timer.started())"),
    20: ("src/object/bullet.cpp", "if (life_count <= 0.f)"),
    21: ("src/object/player.cpp", "if (m_sprite->animation_done()) {"),
    28: ("src/race/race_manager.hpp", "return m_num_finished_karts >= m_player_karts.size();"),
    32: ("src/items/powerup.cpp", "stk_config->m_same_powerup_mode == STKConfig::POWERUP_MODE_SAME"),
    37: ("src/items/powerup.cpp", "if ( m_number <= 0 )"),
    40: ("src/karts/kart.cpp", "if(!m_controls.getAccel() || m_gear >= gear_ratio.size())"),
    42: ("src/karts/kart.cpp", "if(ticks_left > 0)"),
    43: ("src/karts/explosion_animation.cpp", "if (m_kart->getAttachment()->getType() == Attachment::ATTACH_NOTHING)"),
    45: ("src/karts/kart.cpp", "if (m_emitters[m_emitter_id]->getStatus() != SFXBase::SFX_PLAYING)"),
    46: ("src/modes/world_status.cpp", "if (m_race_ticks != -1 && m_count_up_ticks >= m_race_ticks)"),
    53: ("src/PlayerEntity.cpp", "if (playerStatus != playerStatusPlaying || isMoving())"),
    56: ("src/MagnetEntity.cpp", "if (playerDistanceFromTarget > MAGNET_MISSED_DISTANCE)"),
    58: ("src/PlayerEntity.cpp", "if (goldTextTimer > 0.0f) return;"),
    65: ("src/PlayerEntity.cpp", "if (direction != firingDirection) currentFireDelay = fireDelay;"),
    66: ("src/PlayerEntity.cpp", "if (playerStatus == playerStatusUnlocking && facingDirection != unlockingDirection)"),
    68: ("src/PlayerEntity.cpp", "currentFireDelay += fireDelay;"),
    69: ("src/PlayerEntity.cpp", "if (pickupVoiceDelay == 0)"),
    82: ("game.js", "if (scoreChanged && thisFrame - Game.hudAt > 250) {"),
    87: ("game.js", "if (steps > 1) {"),
    88: ("game.js", "if (KEY_STATUS.left) {"),
    89: ("game.js", "if (Game.alienShotsInFlight > 0) return;"),
    91: ("game.js", "this.bulletCounter += 10;"),
    92: ("game.js", "this.bulletCounter += 22;"),
    93: ("game.js", "if (this.time > 50) {"),
    95: ("game.js", "if (this.fireRequested && this.bulletCounter <= 0) {"),
    97: ("src/collision.cpp", "const bool reuse_node_boxes = cinfo_usable && self == cinfo_self &&"),
    98: ("src/collision.cpp", "if (!f.walkable && !(collect_resistance && f.move_resistance))"),
    100: ("src/collision.cpp", "if (nearest_dtime < 0) {"),
    101: ("src/client/camera.cpp", "f32 digfrac = m_view_bobbing_anim;"),
    102: ("src/client/mapblock_mesh.cpp", "m_has_animation = !m_animation_info.empty();"),
    105: ("src/client/sound_maker.cpp", "if (m_player_sound_timer > 0.0f)"),
    106: ("src/server/player_sao.cpp", "if (from_client && hp > 0)"),
    112: ("src/client/localplayer.cpp", "if (slippery >= 1) {"),
    114: ("src/collision.cpp", "accel_f.X = accel_f.Y = 0;"),
    115: ("src/server/player_sao.cpp", "if (head_p != m_last_head_node) {"),
    117: ("src/client/sound_maker.cpp", "if (m_player_moving_timer > 0.0f) {"),
    118: ("src/client/sound_maker.cpp", "if (sm->m_player_leftpunch_sound_id != 0)"),
    120: ("src/client/localplayer.cpp", "m_can_jump = ((touching_ground && m_ground_settle_time >= JUMP_GROUND_SETTLE"),
}

INPUTS = ["row", "game", "branch", "spec", "mutation_condition", "target_phi", "phi_keys",
          "decision_site", "record_sites"]
ANSWERS = ["rater", "decision_site_used", "record_site_used", "q1", "q1_evidence", "q1a",
           "q1a_evidence", "q2", "q2_evidence", "not_reached", "confidence", "minutes", "notes"]


def grep(repo, branch, pattern, paths=None, must=None, word=False):
    """file:line of every line holding `pattern` (and `must`, when given); `word`
    matches whole words only, so EVENT_FIRE does not also find EVENT_FIRE_ATTEMPTED."""
    cmd = (["git", "-C", repo, "grep", "-n", "-F"] + (["-w"] if word else [])
           + [pattern, branch, "--"] + (paths or ["."]))
    out = subprocess.run(cmd, capture_output=True, text=True).stdout
    hits = []
    for line in out.splitlines():
        _, path, num, text = line.split(":", 3)
        if must and must not in text:
            continue
        if text.lstrip().startswith(("//", "/*", "*", "#")):
            continue  # a comment that mentions the name, not a use of it
        hits.append(f"{path}:{num}")
    return hits


def constant(game, repo, branch, event_id):
    """The source's name for an event id, read from the game's own definition."""
    src = subprocess.run(["git", "-C", repo, "show", f"{branch}:{EVENT_DEFS[game]}"],
                         capture_output=True, text=True).stdout
    pat = (rf"^\s*([A-Z_]+):\s*{event_id}\b" if game == "Asteroids"
           else rf"^\s*(EVENT_[A-Z_]+)\s*=\s*{event_id}\b")
    m = re.search(pat, src, re.M)
    return m.group(1) if m else None


def phi_keys(phi):
    keys = []
    for quant in re.finditer(r"(Exists|Forall)\(", phi):
        pass
    quantified = set()
    for m in re.finditer(r"(?:Exists|Forall)\(([^()]*(?:\([^()]*\)[^()]*)*)\)", phi):
        quantified |= set(re.findall(r"\b([A-Za-z_]\w*)\.", m.group(1)))
    for m in re.finditer(r"\b([A-Za-z_]\w*)\.([A-Za-z_][\w.\[\]]*)", phi):
        base, field = m.group(1), m.group(2)
        key = f"{base}.{field}"
        if key in (k for k, _ in keys):
            continue
        if base == "subject":
            kind = "subject"
        elif base == "game_session":
            kind = "session"
        elif base in quantified:
            kind = "collection"
        elif "__" in field.split(".")[0]:
            kind = "local"
        else:
            kind = "member"
        keys.append((key, kind))
    return "; ".join(f"{k} ({kind})" for k, kind in keys)


def record_sites(game, repo, branch, spec):
    if "->" not in spec:
        return "whole-trace: every recorded event"
    pred = spec.split("->")[0].strip().split("(")[0].strip()
    events = json.load(open(EVENT_TYPES / game / "event_types.json"))
    events = events.get("events", events)
    ev = next((e for e in events if e["predicate"] == pred), None)
    if ev is None:
        return f"? (no event for predicate {pred})"
    name, const = ev["name"], constant(game, repo, branch, ev["id"])
    if const is None:
        return f"{name}: ? (id {ev['id']} not defined in {EVENT_DEFS[game]})"
    if game == "Asteroids":
        hits = grep(repo, branch, f"GameRV.EV.{const}", ["game.js"], must="record", word=True)
    else:
        hits = [h for h in grep(repo, branch, const, must="record", word=True)
                if "predicates/" not in h]
    text = f"{const}: " + (", ".join(hits) if hits else "(no direct record call)")
    if game == "FreeMiner" and name in FM_BUS:
        bus, put_pat = FM_BUS[name]
        puts = [h for h in grep(repo, branch, put_pat, ["src"], must="put(")
                if "predicates/" not in h]
        text += f" | via bus MtEvent::{bus}, put at: " + ", ".join(puts)
    return text


def main(out):
    ds = list(csv.reader(open(DATASET), skipinitialspace=True))
    ds = [[c.strip() for c in r] for r in ds[1:]]
    rows = []
    for row, (path, anchor) in sorted(ANCHORS.items()):
        game, branch, spec, _ftype, mc, phi = ds[row - 1][:6]
        repo = REPOS[game]
        hits = grep(repo, branch, anchor, [path])
        rows.append({"row": row, "game": game, "branch": branch, "spec": spec,
                     "mutation_condition": mc, "target_phi": phi, "phi_keys": phi_keys(phi),
                     "decision_site": ", ".join(hits) if hits else f"? (anchor not found in {path})",
                     "record_sites": record_sites(game, repo, branch, spec),
                     **{a: "" for a in ANSWERS}})
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=INPUTS + ANSWERS)
        w.writeheader()
        w.writerows(rows)
    for r in rows:
        print(f"{r['row']:>4} {r['decision_site'][:55]:55s} | {r['record_sites'][:90]}")


if __name__ == "__main__":
    main(sys.argv[1])
