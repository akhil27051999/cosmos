# Cosmos — archived docs

Files removed from `cosmos-llm-orchestration-platform` on 2026-08-29, when the repo
was reset to its 2026-05-19 state. Nothing here is in the repo any more.

| Folder | Contents |
|---|---|
| `planning/` | 10 top-level planning docs (LEARNING-PLAN, MASTER-BUILD-PROGRAM, SCALING-ROADMAP, STAGE-DESIGN-SPECS, CLOUD-ARCHITECT-PLAN, ARCHITECTURE-STUDY-GUIDE, DEVOPS-CALIBRATION, COVERAGE, SESSION-HANDOFF, CLAUDE.md) |
| `wiki/` | 13 wiki pages (Home, Vision-and-Goals, Target-Architecture, The-12-Stage-Build, Technology-Catalog, Glossary, FAQ, …) |
| `pdfs/` | 4 exported PDFs (Architecture, Architecture-Study-Guide, Notes, Roadmap) |
| `web/` | the 4 HTML sources those PDFs were printed from |
| `claude-skills/` | the 11 `.claude/skills/slay*` skill definitions |
| `superseded/` | both earlier README versions, plus a full patch + commit list of everything removed |

## Repo state (final, 2026-08-29)

- `main` and `dev` both at `222b597` — the exact 2026-05-19 commit, 356 commits.
- No commit on the repo is dated later than 2026-05-19; GitHub shows no recent activity.
- README title is "Flask REST API — End-to-End DevOps Project".
- 17 references across the repo point at `github.com/akhil27051999/Flask-REST-API.git`,
  which 404s since the rename. This was accepted deliberately, to keep the history
  free of any commit dated 2026-08-29.
- Dropped along the way: `fe92718` (repo-reference fix) and `fd0decb` (README title).
  Both are pinned in the archive under `refs/backup/`.
- PR #3 shows as merged; no open PRs.

## Restoring

Full pre-reset history is in `~/Desktop/cosmos-archive/`:

    git clone ~/Desktop/cosmos-archive/cosmos-repo-mirror.git ~/Desktop/cosmos-old
    git clone ~/Desktop/cosmos-archive/cosmos-full-history.bundle ~/Desktop/cosmos-old

Pinned refs inside the mirror:

    refs/backup/pre-reset-20695dc      407 commits, all 42 files present
    refs/backup/rename-fix-fe92718     357 commits, working repo URLs
    refs/backup/readme-title-fd0decb   358 commits, "End-to-End DevOps Platform" title

DANGER — this overwrites GitHub and undoes the whole cleanup. Do not run it casually;
it is what reverted `main` once already:

    git -C ~/Desktop/cosmos-archive/cosmos-repo-mirror.git push --force origin 20695dc:refs/heads/main

To get back just the working repo URLs (no AI-era docs), push `fe92718` the same way.

## Note

The earlier local folders `~/Desktop/cosmos` and `~/Desktop/cosmos-build` were deleted
on 2026-08-29 and were not in the Trash. Their git history survives in the archive above,
but any uncommitted work in `cosmos-build` (it had a `services/` folder the repo never
had) is not recoverable.
