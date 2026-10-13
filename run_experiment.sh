#!/usr/bin/env bash
# Controller entrypoint. Thin wrapper around src/run_experiment.py so the
# project root stays free of code — all Python modules live in src/.
# Forwards every argument through unchanged, e.g.:
#
#     ./run_experiment.sh --row 4
#     ./run_experiment.sh --rows 4-9 --manual-auto
#     ./run_experiment.sh --rows 1-60 --skip-manual   # unattended (overnight)
#     ./run_experiment.sh --skip-manual --candidate-sort all  # sweep: one run per sort
#
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python "$HERE/src/run_experiment.py" "$@"
