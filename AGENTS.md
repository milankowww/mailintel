<!--
SPDX-FileCopyrightText: Milan Pikula
SPDX-License-Identifier: EUPL-1.2
-->

# Agent guidance

These instructions apply throughout this repository.
Keep repository-wide rules here. Add a nested `AGENTS.md` only when a directory needs additional guidance, and keep it consistent with the publication rules below.

## Commit and publication rules

- Commit only when the user explicitly requests it for the current work.
- Push only when the user explicitly authorizes pushing the current changes. Earlier permission for a completed push does not authorize later pushes. Requests to implement, fix, test, deploy, or commit do not authorize a push.
- This push rule applies to every remote and branch, including pushes made through tools, scripts, or pull-request creation workflows.
- Force-pushing, rewriting published history, deleting remote branches, and publishing releases each require explicit authorization for that operation.
- Before an authorized commit, review the working tree and staged diff. Include only changes within the authorized commit scope. Leave unrelated work untouched and uncommitted; do not stage it or create a separate commit for it without explicit authorization.
- Treat deployment as a separate action within the user's requested scope. Report whether the running application was updated; a commit or push alone does not deploy it.

## Project context

- Read [README.md](README.md) for the product's purpose, privacy model, and user workflow before changing behavior.
- Follow [CONTRIBUTING.md](CONTRIBUTING.md) for development, testing, migrations, license notices, and contribution credit. Its packaging instructions describe a procedure; they do not grant permission to commit.
- Consult [doc/README.md](doc/README.md) for the relevant architecture, API, installation, and operations documentation as needed.
- Use [doc/installation.md](doc/installation.md) for environment setup and [.github/workflows/ci.yml](.github/workflows/ci.yml) for CI checks. Read the sections relevant to the task rather than loading every document.
- Keep shared procedures in their existing documentation and link to them here. Keep this file focused on durable instructions; save task progress and experiment results elsewhere.
- When changing a documented command or convention, update the affected guidance in the same change. Keep machine-specific paths, service addresses, and local operating instructions outside this public file.

## Where to work

- `backend/mailintel/`: application code, including connectors, analysis services, and persistence.
- `frontend/`: Vue application and frontend tests.
- `tests/backend/`: backend tests.
- `doc/`: user and technical documentation.
- `deploy/` and `scripts/`: deployment configuration and project utilities.

Inspect `git status` and the relevant diff before editing. Preserve existing changes; if they conflict with the requested work, clarify ownership before replacing them. Follow the conventions of the files being changed and keep unrelated refactoring separate.

## Implementation boundaries

- Preserve read-only mailbox access. Keep mailbox credentials with connectors and model outputs as analysis data rather than executable instructions.
- Keep changes focused and follow nearby code conventions. Add abstractions, dependencies, and validation only when the requested behavior needs them.
- Local small models are a supported use case. Keep prompts concise and inference calls economical. Preserve the two-sentence single-message summary target unless the user requests a change.
- For prompt changes, compare against the original using representative samples before claiming an improvement. Report observed tradeoffs and variability.
- Keep credentials, real correspondence, private configuration, and experiment artifacts out of tracked source. Use synthetic data for committed tests. Keep experiments using private mail outside this repository.
- Preserve published migrations; follow the migration procedure in [CONTRIBUTING.md](CONTRIBUTING.md#database-changes) when changing the schema.

## Verification and documentation

- Run checks proportionate to the changed behavior. Start with relevant tests; broaden testing when the scope or failures justify it.
- Add or adjust tests for meaningful behavior changes and regressions. For documentation-only edits, check accuracy and links rather than running application tests.
- With the development Python environment active, backend tests run with `python -m pytest`; pass a test file to limit scope. Frontend checks run from `frontend/` with `node --test tests/*.test.js` and `npm run build`.
- Database tests require a disposable test database as described in [CONTRIBUTING.md](CONTRIBUTING.md#run-checks). Keep them separate from the running instance's data.
- Report what changed, what was actually tested, and any remaining uncertainty. Distinguish automated checks from visual or live-instance verification.
- If a check cannot run, state the blocker. Preserve the check's purpose when fixing failures rather than weakening it to obtain a passing result.
- Update affected user documentation when behavior changes. Explain the current behavior in plain language from the user's perspective, with installation and fuller reference material in their existing locations.
