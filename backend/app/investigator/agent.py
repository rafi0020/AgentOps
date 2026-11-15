"""The Investigator: its instructions and tools. Workers load `AGENT` (see app/worker/run.py)."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select

from app.agent.tools import AgentSpec
from app.db import system_session, tenant_session
from app.investigator import tools  # noqa: F401 - registers the Investigator's own tools
from app.investigator.common import registry
from app.investigator.prompts import SYSTEM_PROMPT
from app.models.identity import Org
from app.models.projects import Project


async def context(org_id: UUID) -> str:
    """Per-run facts, kept out of the system prompt so that stays identical (and cacheable) for everyone."""
    async with system_session() as db:
        name = await db.scalar(select(Org.name).where(Org.id == org_id))
    async with tenant_session(org_id) as db:
        projects = (await db.execute(select(Project.name).order_by(Project.name))).scalars().all()
    return (
        f"Organisation: {name}. Its projects: {', '.join(projects) or 'none'}. Only this organisation's data is visible. "
        f"The time now is {datetime.now(UTC):%Y-%m-%dT%H:%M}Z."
    )


AGENT = AgentSpec(SYSTEM_PROMPT, registry, context=context)
