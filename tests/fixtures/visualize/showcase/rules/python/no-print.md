---
paths: ["src/**/*.py"]
check:
  shell: grep -n 'print(' $WW_STEP_CHANGED_FILES || true
  assert: [empty]
max_fixes: 5
---
Write user-facing output through the console module, never with print().
