---
name: ww-refresh
description: Refresh what ww learned about the project, keeping what still holds, updating what changed and marking what no longer holds as superseded. Use when the operator invokes /ww-refresh or says ww's picture of the project is out of date.
---

# Refresh what ww learned

Refreshing is running the project learning again: its steps read the existing
`.ww/project.md` first and update it in place. ww keeps no other learning
about the operator, their role, team or company; older files of that kind
are left alone and ignored.

1. Run `./ww onboarding --json` and tell the operator when ww last learned
   about the project (`learned.project`; `null` is never), and whether
   `.ww/project.md` exists.
2. If `user.explain` is `true`, add `--mode ww-narrate`. Start the workflow
   with the start command `./ww discover` shows, omitting the task ID unless
   discover says this project needs one:

   ```console
   ./ww start --workflow ww-learn-project --agent <agent> --requirements "Refresh what ww knows about the project; keep what still holds." --role manager
   ```

3. Follow every page until the run completes. It reruns `./ww inspect`,
   re-reads only what may have changed and shows what differs from the file
   before writing it.
