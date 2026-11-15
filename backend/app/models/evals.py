from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import BigInteger, FetchedValue, ForeignKey, Text, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base


def _id():
    return mapped_column(primary_key=True, server_default=func.gen_random_uuid())


class Dataset(Base):
    __tablename__ = "datasets"
    id: Mapped[UUID] = _id()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("orgs.id"))
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"))
    name: Mapped[str]
    description: Mapped[str] = mapped_column(default="")
    created_by: Mapped[UUID | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class DatasetItem(Base):
    __tablename__ = "dataset_items"
    id: Mapped[UUID] = _id()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("orgs.id"))
    dataset_id: Mapped[UUID] = mapped_column(ForeignKey("datasets.id"))
    seq: Mapped[int] = mapped_column(BigInteger, server_default=FetchedValue())  # the order items were added in
    input: Mapped[Any] = mapped_column(JSONB)
    expected: Mapped[Any | None] = mapped_column(JSONB)
    item_metadata: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)
    source_trace_id: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Evaluator(Base):
    __tablename__ = "evaluators"
    id: Mapped[UUID] = _id()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("orgs.id"))
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"))
    name: Mapped[str]
    kind: Mapped[str]
    config: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_by: Mapped[UUID | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Prompt(Base):
    __tablename__ = "prompts"
    id: Mapped[UUID] = _id()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("orgs.id"))
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"))
    name: Mapped[str]
    primary_evaluator_id: Mapped[UUID | None] = mapped_column(ForeignKey("evaluators.id"))
    min_score: Mapped[float | None]
    max_drop: Mapped[float] = mapped_column(default=0.02)
    production_version: Mapped[int | None]
    created_by: Mapped[UUID | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class PromptVersion(Base):
    __tablename__ = "prompt_versions"
    id: Mapped[UUID] = _id()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("orgs.id"))
    prompt_id: Mapped[UUID] = mapped_column(ForeignKey("prompts.id"))
    version: Mapped[int]
    text: Mapped[str]
    variables: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    model_settings: Mapped[dict] = mapped_column(JSONB, default=dict)
    note: Mapped[str | None]
    created_by: Mapped[UUID | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Experiment(Base):
    __tablename__ = "experiments"
    id: Mapped[UUID] = _id()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("orgs.id"))
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"))
    dataset_id: Mapped[UUID] = mapped_column(ForeignKey("datasets.id"))
    name: Mapped[str]
    target: Mapped[dict] = mapped_column(JSONB)
    evaluator_ids: Mapped[list[UUID]] = mapped_column(ARRAY(PGUUID(as_uuid=True)), default=list)
    status: Mapped[str] = mapped_column(default="queued")
    totals: Mapped[dict | None] = mapped_column(JSONB)
    error: Mapped[str | None]
    cost_usd: Mapped[Decimal] = mapped_column(default=Decimal(0))
    items_total: Mapped[int] = mapped_column(default=0)
    items_done: Mapped[int] = mapped_column(default=0)
    cancel_requested: Mapped[bool] = mapped_column(default=False)
    attempts: Mapped[int] = mapped_column(default=0)
    lease_owner: Mapped[str | None]
    lease_expires_at: Mapped[datetime | None]
    created_by: Mapped[UUID | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    finished_at: Mapped[datetime | None]


class ExperimentResult(Base):
    __tablename__ = "experiment_results"
    id: Mapped[UUID] = _id()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("orgs.id"))
    experiment_id: Mapped[UUID] = mapped_column(ForeignKey("experiments.id"))
    item_id: Mapped[UUID] = mapped_column(ForeignKey("dataset_items.id"))
    output: Mapped[Any | None] = mapped_column(JSONB)
    output_hash: Mapped[str | None]
    latency_ms: Mapped[float | None]
    cost_usd: Mapped[Decimal | None]
    error: Mapped[str | None]
    scores: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class HumanLabel(Base):
    __tablename__ = "human_labels"
    id: Mapped[UUID] = _id()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("orgs.id"))
    evaluator_id: Mapped[UUID] = mapped_column(ForeignKey("evaluators.id"))
    item_id: Mapped[UUID] = mapped_column(ForeignKey("dataset_items.id"))
    output_hash: Mapped[str]
    score: Mapped[int]
    labelled_by: Mapped[UUID | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ReleaseRequest(Base):
    __tablename__ = "release_requests"
    id: Mapped[UUID] = _id()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("orgs.id"))
    prompt_id: Mapped[UUID] = mapped_column(ForeignKey("prompts.id"))
    from_version: Mapped[int | None]
    to_version: Mapped[int]
    experiment_id: Mapped[UUID | None] = mapped_column(ForeignKey("experiments.id"))
    gate: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str]
    reason: Mapped[str | None]
    requested_by: Mapped[UUID | None]
    decided_by: Mapped[UUID | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    decided_at: Mapped[datetime | None]
