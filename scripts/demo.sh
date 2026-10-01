#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# A self-contained ww demo, for recording or for a first look.
#
# It builds a throwaway project in a temporary directory, initializes ww in it,
# writes a two-step workflow with a test handler, and runs one task from start
# to completion. Nothing outside the temporary directory is touched.
#
# Usage:
#   scripts/demo.sh                 # run it
#   scripts/demo.sh --keep          # keep the temporary project afterwards
#   asciinema rec ww-demo.cast -c scripts/demo.sh
#
# Requires ww-agentic-workflows on PATH:
#   pipx install --editable .

set -euo pipefail

KEEP=0
[ "${1:-}" = "--keep" ] && KEEP=1

if ! command -v ww-agentic-workflows >/dev/null 2>&1; then
  echo "ww-agentic-workflows is not on PATH. Install it first:" >&2
  echo "  pipx install --editable ." >&2
  exit 1
fi

# Typing pace, so a recording is readable. DEMO_SPEED=0 runs it instantly.
PAUSE=${DEMO_SPEED-1}

project=$(mktemp -d "${TMPDIR:-/tmp}/ww-demo.XXXXXX")
cleanup() {
  if [ "$KEEP" = "1" ]; then
    printf '\nThe demo project is at %s\n' "$project"
  else
    rm -rf "$project"
  fi
}
trap cleanup EXIT

say() {
  printf '\n\033[2m# %s\033[0m\n' "$1"
  sleep "$PAUSE"
}

run() {
  printf '\n\033[1m$ %s\033[0m\n' "$*"
  sleep "$PAUSE"
  "$@"
  sleep "$PAUSE"
}

cd "$project"
git init -q .
git commit -q --allow-empty -m "Initial commit"

say "A fresh project. Initialize ww in it."
run ww-agentic-workflows init --no-input --no-worktrees --no-skills \
  --no-link-instructions --task-format digit

say "Describe the process as steps. This one is two steps and a test handler."
cat > ww.yaml <<'YAML'
handlers:
  - name: tests
    argv: [python3, -c, "print('3 passed')"]

hooks:
  before_complete:
    - workflows: [task]
      steps: [develop]
      handlers:
        - name: tests

workflows:
  - name: task
    description: Implement a small change end to end.
    steps:
      - develop: Implement the requested change.
      - document: Update the documentation the change affects.
YAML
run cat ww.yaml

say "Validate it, without running anything."
run ./ww lint

say "Compile it into the exact plan that would run. Still no task state."
run ./ww plan --workflow task --agent claudecode

say "Your agent would start here. It opens the task with your request."
run ./ww start TASK-1 --workflow task --agent claudecode \
  --requirements "Add retry handling to the upload client." --role manager

say "ww printed the next command. The agent runs it, and gets its instruction."
run ./ww next TASK-1 --role manager

say "The agent does the work, then reports the result back to ww."
run ./ww complete TASK-1 --role worker \
  --artifact "Added exponential backoff to \`UploadClient.send\`." \
  --summary "Retries land in UploadClient.send; document the new backoff settings."

say "The test handler ran inside that completion, and step two is open."
run ./ww complete TASK-1 --role worker \
  --artifact "Documented the backoff settings." \
  --summary "Docs updated."

say "Last step: the built-in run summary."
run ./ww complete TASK-1 --role worker \
  --variable summary="Added retry handling to the upload client and documented it." \
  --artifact "Task complete."

say "Everything the run produced is saved and inspectable."
run ./ww artifacts TASK-1
