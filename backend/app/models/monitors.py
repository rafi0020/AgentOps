from datetime import datetime
from uuid import UUID

from sqlalchemy import ForeignKey, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base


class MonitorRule(Base):
    __tablename__ = "monitor_rules"
    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    org_id: Mapped[UUID] = mapped_column(ForeignKey("orgs.id"))
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"))
    name: Mapped[str]
    metric: Mapped[str]
    evaluator_id: Mapped[UUID | None] = mapped_column(ForeignKey("evaluators.id"))
    filter: Mapped[dict] = mapped_column(JSONB, default=dict)
    comparison: Mapped[str]
    threshold: Mapped[float]
    window_minutes: Mapped[int]
    min_calls: Mapped[int] = mapped_column(default=20)
    status: Mapped[str] = mapped_column(default="active")
    created_by: Mapped[UUID | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class MonitorAlert(Base):
    __tablename__ = "monitor_alerts"
    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    org_id: Mapped[UUID] = mapped_column(ForeignKey("orgs.id"))
    rule_id: Mapped[UUID] = mapped_column(ForeignKey("monitor_rules.id"))
    window_start: Mapped[datetime]
    window_end: Mapped[datetime]
    value: Mapped[float]
    calls: Mapped[int]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    acknowledged_by: Mapped[UUID | None]
    acknowledged_at: Mapped[datetime | None]
    investigation_run_id: Mapped[UUID | None] = mapped_column(ForeignKey("runs.id"))
