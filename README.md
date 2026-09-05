# GitHub Ticket Flow for Hermes

A user-level Hermes plugin that turns an ordered list of GitHub issues into a durable Kanban pipeline:

```text
local analyst context → implementation/Ponytail → local analyst diff summary → independent review → PR → user merge → verification/cleanup
```

## Install

```bash
hermes plugins install nan3333/github-ticket-flow --enable
```

Hermes profiles use isolated homes. Install the plugin for each worker profile that needs it:

```bash
hermes -p analyst plugins install nan3333/github-ticket-flow --enable
hermes -p implementer plugins install nan3333/github-ticket-flow --enable
hermes -p reviewer plugins install nan3333/github-ticket-flow --enable
```

## Usage

From any GitHub checkout:

```bash
hermes ticket-flow start 191 154 156 --dry-run
hermes ticket-flow start 191 154 156
hermes ticket-flow start 191 154 156 --auto-merge
hermes ticket-flow status
hermes ticket-flow detach --board <board> --yes
```

The plugin infers the repository root and `owner/repo` from Git. Standard repositories require no workflow configuration. The default `analyst` profile performs the read-only context and diff-summary passes; `implementer` remains the only writer and `reviewer` remains the independent approval authority.

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
```

Supported top-level override keys are `roles`, `pipeline`, `worktrees`, `stage_skills`, and `gates`. Issue numbers, repository identity, workflow ID, board slug, and project path are inferred per run rather than stored in the repository.

Role defaults are `analyst`, `implementer`, `reviewer`, and `default` for merge verification. `roles.analyst` may point to another read-only profile. The legacy `roles.researcher` key remains accepted for compatibility but is no longer assigned by the default graph.

Each successful start writes the complete effective configuration and card IDs to `ticket-flow-manifest.json` beside the board database.

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
hermes plugins doctor . --ci
```

## License

Licensed under the [Apache License 2.0](LICENSE).
