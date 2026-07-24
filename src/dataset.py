"""Read the benchmark dataset into TestCase rows.

The CSV has these columns: ``Game Name`` (which game the row belongs to — selects
a :class:`config.Game` bundle, and gamerv's active predicate catalog), ``Github
Branch`` (the mutated branch of that game's repo), ``Specification`` (a ``P -> Q``
safety-property body, no leading ``G``), ``Target Phi`` (the ground-truth
refinement ``phi`` to test ``phi -> R``), and an optional ``Positive Source``
(where the good ``P & Q`` states come from — see below).

``Positive Source`` picks step 5's recording strategy per row:
  * ``mutant`` (default; also the value for a blank/absent cell) — record only the
    mutant branch. Its trace supplies BOTH the good (P & Q) and violation (P & ~Q)
    states, so phi is decided within the single mutant branch.
  * ``base`` — record the SAME tapes on both the unmutated ``base`` binary and the
    mutant, pooling both traces. base supplies the good states, the mutant the
    violations. Needed for always-on mutations whose mutant trace has NO good
    states to contrast (single-branch mining returns "no_positives"). This is the
    "baseline-contrast" path in the pipeline.

``csv.DictReader`` handles the doubled-quote escaping in the file, so a cell
written as ``"Walk -> ObjectPresent(""Tux"")"`` comes back as the plain string
``Walk -> ObjectPresent("Tux")``.
"""

import csv
from dataclasses import dataclass
from pathlib import Path

import config


@dataclass(frozen=True)
class TestCase:
    game: str                 # Game Name (resolved to a config.Game in the pipeline)
    specification: str        # P -> Q body (no G)
    branch: str               # github/mutation branch name
    phi: str                  # ground-truth refinement
    row: int                  # 1-based row number (after the header)
    positive_source: str = "mutant"  # where P & Q comes from: "mutant" | "base"
    correct_drift: bool = False      # replay with drift correction on (see below)

    @property
    def formula(self) -> str:
        """The gamerv safety property: the spec wrapped in a global G(...)."""
        return f"G({self.specification})"

    @property
    def baseline_contrast(self) -> bool:
        """True when the good (P & Q) states come from the base binary, so step 5
        records the same tapes on base AND the mutant and pools both traces."""
        return self.positive_source == "base"


# Column headers, matched case-insensitively / whitespace-trimmed so a small
# header tweak in the CSV doesn't silently drop every row.
_GAME_KEYS = {"game name", "game"}
_SPEC_KEYS = {"specification", "spec", "property"}
_BRANCH_KEYS = {"github branch", "branch"}
_PHI_KEYS = {"target phi", "phi", "ground truth"}
_POSSRC_KEYS = {"positive source", "positive", "contrast"}

# Accepted values for the Positive Source column, mapped to the canonical token.
# Blank / unknown -> "mutant" (record the mutant alone, today's default).
_POSSRC_VALUES = {
    "mutant": "mutant", "self": "mutant", "": "mutant",
    "base": "base", "baseline": "base", "base+mutant": "base",
}


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
            # Optional column; absent header or blank cell -> "mutant" (record the
            # mutant alone). Unrecognised values also fall back to "mutant".
            pos_raw = _pick(rowdict, _POSSRC_KEYS, default="").lower()
            positive_source = _POSSRC_VALUES.get(pos_raw, "mutant")
            if not (spec and branch):
                continue  # skip blank / not-yet-filled lines
            cases.append(TestCase(game, spec, branch, phi, i, positive_source))
    return cases


if __name__ == "__main__":  # quick manual check
    for c in read_cases()[:5]:
        print(c.row, c.game, "|", c.branch, "|", c.formula, "|  phi =", c.phi)
