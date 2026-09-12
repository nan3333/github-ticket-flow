# GitHub Ticket Flow for Hermes

A user-level Hermes plugin that turns an ordered list of GitHub issues into a durable Kanban pipeline:

```text
research → implementation/Ponytail → independent review → PR → user merge → verification/cleanup
```

## Install

```bash
hermes plugins install nan3333/github-ticket-flow --enable
```

Hermes profiles use isolated homes. Install the plugin for each worker profile that needs it:

```bash
hermes -p researcher plugins install nan3333/github-ticket-flow --enable
hermes -p implementer plugins install nan3333/github-ticket-flow --enable
hermes -p reviewer plugins install nan3333/github-ticket-flow --enable
```

## Usage

From any GitHub checkout:

```bash
hermes ticket-flow start 191 154 156 --dry-run
hermes ticket-flow start 191 154 156
hermes ticket-flow start 191 154 156 --runner herdr
hermes ticket-flow start 191 154 156 --auto-merge
hermes ticket-flow status
hermes ticket-flow detach --board <board> --yes
```

The plugin infers the repository root and `owner/repo` from Git. Standard repositories require no workflow configuration.

The default run stops at each PR for user merge confirmation. For an autonomous run, `--auto-merge` keeps independent review and final gates but lets the merge card merge after required GitHub checks pass and the approved head/digest are unchanged. The selected policy is recorded in the run manifest.

Use the flag for a single run. To make autonomous merge the repository default, set `pipeline.require_user_merge_confirmation: false` in `.hermes/ticket-flow.yaml`; a run without the flag otherwise remains manual.

## Optional repository overrides

When present, `<repo>/.hermes/ticket-flow.yaml` may override only shared defaults:

```yaml
gates:
  final:
    - npm run typecheck
    - npx eslint src scripts --max-warnings=0
    - npm test
parallelization:
  research:
    max_workers: 3
  review:
    max_workers: 3
  tests:
    max_workers: 2
    # Only an ordered prefix of gates.final may run concurrently.
    # List together only commands that share no database or build output.
    final_gate_groups:
      - [npm run typecheck, npx eslint src scripts --max-warnings=0]
execution:
  runner: herdr
  poll_interval_seconds: 2
  max_spawn: 4
```

Supported top-level override keys are `roles`, `pipeline`, `execution`, `worktrees`, `stage_skills`, `gates`, and `parallelization`. Issue numbers, repository identity, workflow ID, board slug, and project path are inferred per run rather than stored in the repository.

Research and review default to one bounded batch of three read-only delegation lenses. Their parent worker verifies the returned evidence and remains the only Kanban lifecycle owner. Test execution defaults to two concurrent commands, but final gates remain serial unless `final_gate_groups` explicitly names a prefix of `gates.final`; commands that share a database or build output must not be grouped.

Each successful start writes the complete effective configuration and card IDs to `ticket-flow-manifest.json` beside the board database.

### Visible Herdr execution

`execution.runner: herdr` keeps Hermes Kanban as the source of truth while dispatching each card into a named Herdr workspace. Start the workflow from a local Herdr-managed terminal whose panes share the repository filesystem. The command opens a dedicated runner tab; top-level cards get visible workspaces, and research/review lenses get named tabs within them. Herdr workspace and pane IDs are written as a structured task comment in this visibility baseline.

Disable the gateway dispatcher before starting a Herdr-backed flow so two dispatchers cannot race:

```bash
hermes config set kanban.dispatch_in_gateway false
hermes gateway restart
```

To transfer an existing board, run `hermes ticket-flow herdr-run --board <slug>` from a Herdr-managed terminal. `hermes -p default ticket-flow herdr-delegate --spec <path.json>` is reserved for workers running nested read-only lenses; generated card bodies carry the bounded spec contract.

Implementation cards also request the `ponytail` skill. Install that skill in the implementer profile or override `stage_skills.delivery` in the optional repository configuration.

## Update or remove

```bash
hermes plugins update github-ticket-flow
hermes plugins remove github-ticket-flow
```

## Development verification

```bash
python3 tests/test_ticket_flow.py -v
python3 tests/test_cli.py -v
python3 tests/test_herdr_runner.py -v
hermes plugins doctor . --ci
```

## License

Licensed under the [Apache License 2.0](LICENSE).
