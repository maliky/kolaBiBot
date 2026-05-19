#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<EOF
Usage: $(basename "$0") [-e EXCLUDE_FILE] [-o OUTPUT_PNG]

Generate a UML class diagram for the kolabi package.
  -e  Path to newline-delimited list of modules/classes to exclude
      (default: scripts/models_to_exclude.txt)
  -o  Output PNG path (default: Diagrams/kolabi_models.png)
EOF
}

exclude_file="scripts/models_to_exclude.txt"
output_png="Diagrams/kolabi_models.png"

while getopts ":e:o:h" opt; do
  case "$opt" in
    e) exclude_file="$OPTARG" ;;
    o) output_png="$OPTARG" ;;
    h) usage; exit 0 ;;
    *) usage; exit 1 ;;
  esac
done

if [[ ! -f "$exclude_file" ]]; then
  echo "Exclude list not found: $exclude_file" >&2
  exit 1
fi

if ! command -v pyreverse >/dev/null 2>&1; then
  echo "pyreverse not found. Install pylint (pip install pylint)." >&2
  exit 1
fi

mkdir -p "$(dirname "$output_png")"

ignore_list=$(paste -sd, "$exclude_file")
tmp_dir=$(mktemp -d)
trap 'rm -rf "$tmp_dir"' EXIT

pushd "$tmp_dir" >/dev/null
pyreverse -o png -p kolabi_models \
  --ignore "$ignore_list" \
  ../../kolabi >/dev/null
popd >/dev/null

src_png="$tmp_dir/classes_kolabi_models.png"
if [[ ! -f "$src_png" ]]; then
  echo "pyreverse did not produce $src_png" >&2
  exit 1
fi

mv "$src_png" "$output_png"
echo "Diagram written to $output_png"
