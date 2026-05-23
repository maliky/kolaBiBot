#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<EOF
Usage: $(basename "$0") [OPTIONS] [OUTPUT_DIR]

Generate architecture diagrams for the kolabi package.

Default output directory:
  ./Docs

Generated files:
  - classes_kolabi.plantuml
  - packages_kolabi.plantuml
  - botmap.org (PlantUML Babel blocks exporting to Map/)
  - imports_kolabi.svg
  - optional PDF exports from PlantUML sources (via SVG conversion)

Optional exclude file:
  ./scripts/modules_to_exclude.txt

Options:
  --pdf                Render PlantUML files to SVG and convert to PDF.
  --summary            Use class-name-only mode (much smaller class diagrams).
  --split              Generate per-subpackage class diagrams.
  --no-imports         Skip pydeps import graph generation.
  --org-only           Generate/update botmap.org from PlantUML files and skip render steps.
  --poster SCALE       Run pdfposter on generated PDFs with linear SCALE.
                       Example: --poster 2.0 (prints across multiple sheets).
  -h, --help           Show this help.
EOF
}

want_pdf=0
summary_mode=0
split_mode=0
skip_imports=0
org_only=0
poster_scale=""
outdir="./Docs"
exclude_file="./scripts/modules_to_exclude.txt"

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help)
      usage
      exit 0
      ;;
    --pdf)
      want_pdf=1
      shift
      ;;
    --summary)
      summary_mode=1
      shift
      ;;
    --split)
      split_mode=1
      shift
      ;;
    --no-imports)
      skip_imports=1
      shift
      ;;
    --org-only)
      org_only=1
      shift
      ;;
    --poster)
      shift
      if [[ $# -eq 0 ]]; then
        echo "Missing value for --poster" >&2
        exit 1
      fi
      poster_scale="$1"
      shift
      ;;
    *)
      outdir="$1"
      shift
      ;;
  esac
done

mkdir -p "$outdir"
mkdir -p "$outdir/Map"

if ! command -v pyreverse >/dev/null 2>&1; then
  echo "Missing pyreverse. Install with: pip install pylint" >&2
  exit 1
fi

if ! command -v pydeps >/dev/null 2>&1; then
  echo "Missing pydeps. Install with: pip install pydeps graphviz" >&2
  exit 1
fi

annotate_plantuml_roles() {
  local target_file="$1"
  local diagram_kind="$2"

if [[ ! -f "$target_file" ]]; then
    return
  fi

  python - "$target_file" "$diagram_kind" <<'PY'
from __future__ import annotations

import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
kind = sys.argv[2]
text = path.read_text(encoding="utf-8")

def role_for_alias(alias: str) -> str | None:
    if alias.startswith(("kolabi.bot.chronos", "kolabi.bot.strategy_runtime", "kolabi.bot.service")):
        return "Chronos"
    if alias.startswith(("kolabi.bot.orange",)):
        return "Orange"
    if alias.startswith(("kolabi.bot.isis", "kolabi.bot.pair_cycle", "kolabi.bot.domain")):
        return "Isis"
    if alias.startswith(("kolabi.bot.janus", "kolabi.bot.order_building")):
        return "Janus"
    if alias.startswith(("kolabi.runtime.kola.ogun_executor", "kolabi.shared.exchanges")):
        return "Ogun"
    return None

if kind == "classes":
    pattern = re.compile(r'^(class\s+".*?"\s+as\s+(\S+))\s+\{', re.MULTILINE)
elif kind == "packages":
    pattern = re.compile(r'^(package\s+".*?"\s+as\s+(\S+))\s+\{', re.MULTILINE)
else:
    raise SystemExit(0)

def repl(match: re.Match[str]) -> str:
    lead = match.group(1)
    alias = match.group(2)
    role = role_for_alias(alias)
    if role is None:
        return match.group(0)
    return f"{lead} <<{role}>> {{"

text = pattern.sub(repl, text)

legend_block = """
skinparam class {
  BackgroundColor<<Chronos>> #DDEBFF
  BorderColor<<Chronos>> #2F5FA9
  BackgroundColor<<Orange>> #FFE8CC
  BorderColor<<Orange>> #CC7A00
  BackgroundColor<<Isis>> #DDF3E2
  BorderColor<<Isis>> #2E8B57
  BackgroundColor<<Janus>> #E7DDF9
  BorderColor<<Janus>> #6B46C1
  BackgroundColor<<Ogun>> #FBD6C6
  BorderColor<<Ogun>> #B45309
}
skinparam package {
  BackgroundColor<<Chronos>> #EEF4FF
  BorderColor<<Chronos>> #2F5FA9
  BackgroundColor<<Orange>> #FFF3E0
  BorderColor<<Orange>> #CC7A00
  BackgroundColor<<Isis>> #ECFAF0
  BorderColor<<Isis>> #2E8B57
  BackgroundColor<<Janus>> #F4EEFF
  BorderColor<<Janus>> #6B46C1
  BackgroundColor<<Ogun>> #FFEADF
  BorderColor<<Ogun>> #B45309
}
legend right
|= Layer |= Meaning |
|<#DDEBFF> Chronos | Supervisor / runtime shell |
|<#FFE8CC> Orange | Event normalisation / ingestion |
|<#DDF3E2> Isis | Strategy reducer / pure transitions |
|<#E7DDF9> Janus | Intent to command planner |
|<#FBD6C6> Ogun | Execution boundary / adapters |
endlegend
"""

if legend_block not in text:
    if "@enduml" in text:
        text = text.replace("@enduml", legend_block + "\n@enduml")
    else:
        text += "\n" + legend_block + "\n"

path.write_text(text, encoding="utf-8")
PY
}

write_org_map() {
  local org_file="$outdir/botmap.org"
  shift
  local puml_files=("$@")
  local rel_map_dir="Map"
  if [[ "$outdir" != "." && "$outdir" != "./" ]]; then
    rel_map_dir="$outdir/Map"
  fi

  {
    echo "#+TITLE: kolabi bot map"
    echo "#+PROPERTY: header-args:plantuml :results file link :exports both"
    echo
    echo "* Overview"
    echo "Generated PlantUML source blocks exporting images to =${rel_map_dir}/=."
    echo
    for puml in "${puml_files[@]}"; do
      local base stem out_file section
      base="$(basename "$puml")"
      stem="${base%.plantuml}"
      out_file="${rel_map_dir}/${stem}.svg"
      section="${stem//_/ }"
      echo "* ${section}"
      echo "#+BEGIN_SRC plantuml :file ${out_file}"
      cat "$puml"
      echo "#+END_SRC"
      echo
    done
  } >"$org_file"
}

ignore_args=()
if [[ -f "$exclude_file" ]]; then
  ignore_list="$(paste -sd, "$exclude_file")"
  if [[ -n "$ignore_list" ]]; then
    ignore_args=(--ignore "$ignore_list")
  fi
fi

tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT

echo "Generating class/package UML with pyreverse..."

pyreverse_opts=(
  -o plantuml
  -p kolabi
)
if [[ "$summary_mode" -eq 1 ]]; then
  pyreverse_opts+=(-k)
fi

pyreverse \
  "${pyreverse_opts[@]}" \
  "${ignore_args[@]}" \
  kolabi \
  >/dev/null

mv classes_kolabi.plantuml "$outdir/classes_kolabi.plantuml"
mv packages_kolabi.plantuml "$outdir/packages_kolabi.plantuml"
annotate_plantuml_roles "$outdir/classes_kolabi.plantuml" "classes"
annotate_plantuml_roles "$outdir/packages_kolabi.plantuml" "packages"

if [[ "$split_mode" -eq 1 ]]; then
  echo "Generating split class diagrams by top subpackage..."
  rm -f "$outdir"/classes_kolabi_*.plantuml "$outdir"/packages_kolabi_*.plantuml
  mapfile -t subpackages < <(
    find kolabi -mindepth 1 -maxdepth 1 -type d \
      ! -name '__pycache__' \
      ! -name '.*' \
      -printf '%f\n' | sort
  )
  for sub in "${subpackages[@]}"; do
    pyreverse \
      "${pyreverse_opts[@]}" \
      -p "kolabi_${sub}" \
      "${ignore_args[@]}" \
      "kolabi.${sub}" \
      >/dev/null
    mv "classes_kolabi_${sub}.plantuml" "$outdir/classes_kolabi_${sub}.plantuml"
    mv "packages_kolabi_${sub}.plantuml" "$outdir/packages_kolabi_${sub}.plantuml"
    annotate_plantuml_roles "$outdir/classes_kolabi_${sub}.plantuml" "classes"
    annotate_plantuml_roles "$outdir/packages_kolabi_${sub}.plantuml" "packages"
  done
fi

org_sources=(
  "$outdir/classes_kolabi.plantuml"
  "$outdir/packages_kolabi.plantuml"
)
if [[ "$split_mode" -eq 1 ]]; then
  while IFS= read -r file; do
    org_sources+=("$file")
  done < <(
    find "$outdir" -maxdepth 1 -type f \( -name 'classes_kolabi_*.plantuml' -o -name 'packages_kolabi_*.plantuml' \) | sort
  )
fi
write_org_map "${org_sources[@]}"

if [[ "$org_only" -eq 1 ]]; then
  echo "Generated Org map only: $outdir/botmap.org"
  exit 0
fi

if [[ "$skip_imports" -eq 0 ]]; then
  echo "Generating import dependency graph with pydeps..."

  pydeps kolabi \
    --cluster \
    --max-bacon=3 \
    --show-deps \
    --noshow \
    -o "$outdir/imports_kolabi.svg"
else
  echo "Skipping import dependency graph (--no-imports)."
fi

if [[ "$want_pdf" -eq 1 ]]; then
  if ! command -v plantuml >/dev/null 2>&1; then
    echo "Missing plantuml executable." >&2
    exit 1
  fi
  if ! command -v inkscape >/dev/null 2>&1; then
    echo "Missing inkscape for PDF conversion fallback." >&2
    exit 1
  fi

  echo "Generating SVG from PlantUML sources..."
  plantuml_inputs=(
    "$outdir/classes_kolabi.plantuml"
    "$outdir/packages_kolabi.plantuml"
  )
  if [[ "$split_mode" -eq 1 ]]; then
    while IFS= read -r file; do
      plantuml_inputs+=("$file")
    done < <(
      find "$outdir" -maxdepth 1 -type f \( -name 'classes_kolabi_*.plantuml' -o -name 'packages_kolabi_*.plantuml' \) | sort
    )
  fi
  plantuml -tsvg "${plantuml_inputs[@]}"

  echo "Converting SVG to PDF (prevents truncated direct PlantUML PDF output)..."
  while IFS= read -r svg_file; do
    pdf_file="${svg_file%.svg}.pdf"
    inkscape "$svg_file" --export-type=pdf --export-filename="$pdf_file" >/dev/null
  done < <(find "$outdir" -maxdepth 1 -type f -name '*.svg' | sort)

  if [[ -n "$poster_scale" ]]; then
    if ! command -v pdfposter >/dev/null 2>&1; then
      echo "Missing pdfposter for poster splitting." >&2
      exit 1
    fi
    echo "Generating poster-split PDFs with pdfposter scale=$poster_scale..."
    while IFS= read -r pdf_file; do
      [[ "$pdf_file" == *.poster.pdf ]] && continue
      poster_file="${pdf_file%.pdf}.poster.pdf"
      pdfposter -s "$poster_scale" "$pdf_file" "$poster_file" >/dev/null
    done < <(find "$outdir" -maxdepth 1 -type f -name '*.pdf' | sort)
  fi
fi

echo "Architecture diagrams written to: $outdir"
