"""GitHub ticket-flow plugin registration."""

from pathlib import Path

from .cli import register_cli, ticket_flow_command


def register(ctx) -> None:
    skill_path = Path(__file__).parent / "skills" / "workflow" / "SKILL.md"
    ctx.register_skill(
        name="workflow",
        path=skill_path,
        description="Run durable, serialized GitHub ticket delivery.",
    )
    ctx.register_cli_command(
        name="ticket-flow",
        help="Create and inspect durable GitHub ticket workflows",
        setup_fn=register_cli,
        handler_fn=ticket_flow_command,
        description=(
            "Infers the current GitHub repository, compiles ordered issues into "
            "research, delivery, review, and merge-verification Kanban stages, "
            "and stores the resolved run manifest with the board."
        ),
    )
