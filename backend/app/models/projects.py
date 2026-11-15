from datetime import datetime
from uuid import UUID

from sqlalchemy import ForeignKey, LargeBinary, Text, func
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base


class Project(Base):
    """One application being observed."""

    __tablename__ = "projects"
    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    org_id: Mapped[UUID] = mapped_column(ForeignKey("orgs.id"))
    name: Mapped[str]
    retention_days: Mapped[int] = mapped_column(default=30)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class IngestKey(Base):
    """A credential an application uses to send traces (and, if granted, to read live prompts)."""

    __tablename__ = "ingest_keys"
    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    org_id: Mapped[UUID] = mapped_column(ForeignKey("orgs.id"))
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"))
    name: Mapped[str]
    key_hash: Mapped[str] = mapped_column(unique=True)
    prefix: Mapped[str]
    scopes: Mapped[list[str]] = mapped_column(ARRAY(Text))
    created_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    revoked_at: Mapped[datetime | None]
    last_used_at: Mapped[datetime | None]


class IngestBatch(Base):
    """A received trace batch, stored as it arrived, waiting for an ingest worker."""

    __tablename__ = "ingest_batches"
    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    org_id: Mapped[UUID] = mapped_column(ForeignKey("orgs.id"))
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"))
    payload: Mapped[bytes] = mapped_column(LargeBinary)
    content_type: Mapped[str]
    span_count: Mapped[int]
    status: Mapped[str] = mapped_column(default="pending")
    attempts: Mapped[int] = mapped_column(default=0)
    lease_owner: Mapped[str | None]
    lease_expires_at: Mapped[datetime | None]
    error: Mapped[str | None]
    received_at: Mapped[datetime] = mapped_column(server_default=func.now())
