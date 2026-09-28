#!/usr/bin/env bash
# Build the boards of this package from the vendor's STEP archives.
#
#   tools/regenerate.sh [part ...]      # all parts of tools/boards.tsv when none is named
#
# Writes <part>.step and <part>.json (the report: what was done, and the PCB
# holes gen_yaml.py declares as ports) into $OUT (default: build/). The STEP
# files are not committed: CI publishes them as release assets and
# partcad.yaml points at those (see .github/workflows/release.yml).
#
# Needs the Python packages of tools/requirements.txt; set PYTHON to use one
# other than `python3`. Vendor archives are cached in $CACHE (default
# ~/.cache/nuc-step). JOBS boards are built at a time (default 1); NPROC sets
# the worker processes per board (default: number of CPUs).
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
pkg="$(dirname "$here")"
PYTHON="${PYTHON:-python3}"
CACHE="${CACHE:-$HOME/.cache/nuc-step}"
OUT="${OUT:-$pkg/build}"
JOBS="${JOBS:-1}"
export NPROC="${NPROC:-$(nproc)}"
mkdir -p "$CACHE/zip" "$CACHE/src" "$CACHE/work" "$OUT"
OUT="$(cd "$OUT" && pwd)"

want=" $* "
grep -v '^#' "$here/boards.tsv" | while IFS=$'\t' read -r name url file; do
  [ -z "$name" ] && continue
  if [ "$#" -gt 0 ] && [[ "$want" != *" $name "* ]]; then continue; fi
  zip="$CACHE/zip/$(basename "$url")"
  [ -s "$zip" ] || curl -sfL --retry 3 -A "Mozilla/5.0" -o "$zip" "$url"
  [ -s "$CACHE/src/$file" ] || unzip -oq "$zip" "$file" -d "$CACHE/src"
  printf '%s\t%s\n' "$name" "$file"
done | xargs -P "$JOBS" -d '\n' -I{} bash -c '
  name="${1%%	*}"; file="${1#*	}"
  if "$0" "$2/nuc_envelope.py" "$3/src/$file" "$4/$name.step" "$4/$name.json" \
      --work "$3/work" > "$4/$name.log" 2>&1; then
    echo "$name: ok"
  else
    echo "$name: FAILED" >&2; tail -20 "$4/$name.log" >&2; exit 255
  fi
' "$PYTHON" {} "$here" "$CACHE" "$OUT"
