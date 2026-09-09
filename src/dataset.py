"""Read the benchmark dataset into TestCase rows.

The CSV has these columns: ``Game Name`` (which game the row belongs to — selects
a :class:`config.Game` bundle, and gamerv's active predicate catalog), ``Github
Branch`` (the mutated branch of that game's repo), ``Specification`` (a ``P -> Q``
safety-property body, no leading ``G``), ``Fault Type`` (which of the benchmark's
twelve bug categories the mutation belongs to — see :data:`FAULT_TYPES` and
:func:`fault_class`), and ``Target Phi`` (the ground-truth refinement ``phi`` to
test ``phi -> R``).

``Target Phi`` may be blank: such a row still runs (it is built, recorded and
mined, so it reports an R and a candidate count) but has no ground truth to
decide, and the pipeline reports it ``SKIPPED`` rather than comparing an empty
phi. Only a blank ``Specification`` or ``Github Branch`` drops a row entirely.

Every row is a mutant case: step 5 records the mutant branch alone, and its trace
supplies BOTH the good (P & Q) and violation (P & ~Q) states, so phi is decided
within that single branch.

``csv.DictReader`` handles the doubled-quote escaping in the file, so a cell
written as ``"Walk -> ObjectPresent(""Tux"")"`` comes back as the plain string
``Walk -> ObjectPresent("Tux")``.
"""

import csv
from dataclasses import dataclass
from pathlib import Path

import config

# --------------------------------------------------------------------------
# The fault taxonomy: twelve bug categories under two parent categories.
# --------------------------------------------------------------------------
# The ``Fault Type`` cell names one of these, and results are reported per
# category *and* per parent. The split is the benchmark's own: the first seven
# categories are faults of a single state (the snapshot at one event is already
# wrong), the last five are faults of a state *sequence* (no single snapshot is
# wrong; the order/timing across states is). Kept in dataset order, since that is
# the order the CSV — and every table read off it — walks them in.
NON_TEMPORAL = "Non-temporal fault"
TEMPORAL = "Temporal fault"

NON_TEMPORAL_FAULTS = (
    "Object out of bounds for any state",
    "Object out of bounds for specific state",
    "Invalid Graphical Representation",
    "Invalid Value Change",
    "Lack of information",
    "Action when not allowed",
    "Action not possible",
)
TEMPORAL_FAULTS = (
    "Invalid position over time",
    "Invalid context state over time",
    "Invalid event occurrence over time",
    "Interupted event",          # the dataset's spelling; kept verbatim
    "Implementation response issues",
)
#: Every category, in dataset order.
FAULT_TYPES = NON_TEMPORAL_FAULTS + TEMPORAL_FAULTS

#: Lookup, on a case/whitespace-insensitive key so a hand-edited cell (an extra
#: space, a capitalised word) still lands in its parent rather than silently
#: becoming uncategorised.
_PARENT_BY_KEY = {
    **{" ".join(f.lower().split()): NON_TEMPORAL for f in NON_TEMPORAL_FAULTS},
    **{" ".join(f.lower().split()): TEMPORAL for f in TEMPORAL_FAULTS},
}

#: What a row with no (or an unrecognised) ``Fault Type`` is reported under. It is
#: named rather than dropped, so a typo in the CSV shows up as a row to fix instead
#: of quietly leaving the per-category totals short.
UNCATEGORISED = "(uncategorised)"


def fault_class(fault_type: str) -> str:
    """The parent category of a ``Fault Type`` cell — :data:`NON_TEMPORAL` or
    :data:`TEMPORAL`, or :data:`UNCATEGORISED` for a blank/unknown one."""
    return _PARENT_BY_KEY.get(" ".join((fault_type or "").lower().split()),
                              UNCATEGORISED)


def fault_order(fault_type: str) -> int:
    """Sort key putting the categories in dataset order (unknown ones last)."""
    key = " ".join((fault_type or "").lower().split())
    for i, name in enumerate(FAULT_TYPES):
        if " ".join(name.lower().split()) == key:
            return i
    return len(FAULT_TYPES)


@dataclass(frozen=True)
class TestCase:
    game: str                 # Game Name (resolved to a config.Game in the pipeline)
    specification: str        # P -> Q body (no G)
    branch: str               # github/mutation branch name
    phi: str                  # ground-truth refinement
    row: int                  # 1-based row number (after the header)
    fault_type: str = ""      # Fault Type (one of FAULT_TYPES; "" when absent)
    correct_drift: bool = True       # replay with drift correction on (see below)

    @property
    def formula(self) -> str:
        """The gamerv safety property: the spec wrapped in a global G(...)."""
        return f"G({self.specification})"

    @property
    def fault_class(self) -> str:
        """The row's parent category (see :func:`fault_class`)."""
        return fault_class(self.fault_type)


# Column headers, matched case-insensitively / whitespace-trimmed so a small
# header tweak in the CSV doesn't silently drop every row.
_GAME_KEYS = {"game name", "game"}
_SPEC_KEYS = {"specification", "spec", "property"}
_BRANCH_KEYS = {"github branch", "branch"}
_PHI_KEYS = {"target phi", "phi", "ground truth"}
_FAULT_KEYS = {"fault type", "fault category", "bug category"}
_DRIFT_KEYS = {"replay correct", "correct drift", "drift correct"}

# Cell values that turn drift correction OFF. It is ON by default, so a blank
# cell or a missing column leaves it on and only an explicit falsy value opts a
# row out.
_FALSY = {"0", "false", "no", "n", "off"}


_MISSING = object()


def _pick(rowdict: dict, keys: set[str], default=_MISSING) -> str:
    for k, v in rowdict.items():
        if k is not None and k.strip().lower() in keys:
            return (v or "").strip()
    if default is not _MISSING:
        return default
    raise KeyError(f"dataset is missing one of columns {sorted(keys)}")


def read_cases(path: Path | None = None) -> list[TestCase]:
    path = Path(path) if path is not None else config.DATASET
    cases: list[TestCase] = []
    with open(path, newline="", encoding="utf-8") as f:
        # skipinitialspace: the csv module only honours a field's opening quote
        # when it *immediately* follows the delimiter. A natural hand-edit like
        # ``, "P -> Q"`` (a space after the comma) would otherwise be read
        # literally, so a comma inside the cell splits the field and shifts the
        # later columns by one.
        reader = csv.DictReader(f, skipinitialspace=True)
        for i, rowdict in enumerate(reader, start=1):
            # Game Name is optional in the header (older single-game datasets);
            # an absent column yields "" and the pipeline reports the row's game
            # as unknown rather than silently mis-building.
            game = _pick(rowdict, _GAME_KEYS, default="")
            spec = _pick(rowdict, _SPEC_KEYS)
            branch = _pick(rowdict, _BRANCH_KEYS)
            phi = _pick(rowdict, _PHI_KEYS)
            # Optional: an older dataset without the column reports every row as
            # uncategorised rather than failing to read.
            fault = _pick(rowdict, _FAULT_KEYS, default="")
            # Replay with the game's drift correction on (it snaps the kart
            # back onto the recorded transform when it diverges). ON by
            # default: a row records and replays the SAME mutant branch, so
            # there is no build-to-build divergence for the snap to hide, only
            # the replay's own FP drift and the state a tape cannot carry -
            # which is exactly what correction is for. A row whose fault IS a
            # trajectory deviation the recording did not capture can opt out
            # with a falsy "Replay Correct" cell; see Game.replay_correct_flag.
            drift = _pick(rowdict, _DRIFT_KEYS,
                          default="").lower() not in _FALSY
            if not (spec and branch):
                continue  # skip blank / not-yet-filled lines
            cases.append(TestCase(game, spec, branch, phi, i,
                                  fault_type=fault, correct_drift=drift))
    return cases


if __name__ == "__main__":  # quick manual check
    for c in read_cases()[:5]:
        print(c.row, c.game, "|", c.branch, "|", c.formula, "|  phi =", c.phi,
              "|", c.fault_type, f"({c.fault_class})")
