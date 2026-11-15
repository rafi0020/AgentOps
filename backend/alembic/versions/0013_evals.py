"""Datasets, evaluators, human labels, experiments, prompts and release requests."""

from alembic import op

from app.rls import enable_rls

revision = "0013"
down_revision = "0012"

ORG = "org_id uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE"
PROJECT = "project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE"
ID = "id uuid PRIMARY KEY DEFAULT gen_random_uuid()"
BY = "created_by uuid REFERENCES users(id) ON DELETE SET NULL, created_at timestamptz NOT NULL DEFAULT now()"

TABLES = {
    "datasets": f"{ID}, {ORG}, {PROJECT}, name text NOT NULL, description text NOT NULL DEFAULT '', {BY}, UNIQUE (project_id, name)",
    "dataset_items": f"""{ID}, {ORG}, dataset_id uuid NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
        seq bigint GENERATED ALWAYS AS IDENTITY, input jsonb NOT NULL, expected jsonb,
        metadata jsonb NOT NULL DEFAULT '{{}}', source_trace_id text,
        created_at timestamptz NOT NULL DEFAULT now()""",
    "evaluators": f"""{ID}, {ORG}, {PROJECT}, name text NOT NULL, kind text NOT NULL, config jsonb NOT NULL DEFAULT '{{}}',
        {BY}, UNIQUE (project_id, name)""",
    "prompts": f"""{ID}, {ORG}, {PROJECT}, name text NOT NULL, primary_evaluator_id uuid REFERENCES evaluators(id) ON DELETE SET NULL,
        min_score double precision, max_drop double precision NOT NULL DEFAULT 0.02, production_version int,
        {BY}, UNIQUE (project_id, name)""",
    "prompt_versions": f"""{ID}, {ORG}, prompt_id uuid NOT NULL REFERENCES prompts(id) ON DELETE CASCADE, version int NOT NULL,
        text text NOT NULL, variables text[] NOT NULL DEFAULT '{{}}', model_settings jsonb NOT NULL DEFAULT '{{}}', note text,
        {BY}, UNIQUE (prompt_id, version)""",
    "experiments": f"""{ID}, {ORG}, {PROJECT}, dataset_id uuid NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
        name text NOT NULL, target jsonb NOT NULL, evaluator_ids uuid[] NOT NULL DEFAULT '{{}}',
        status text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','running','completed','failed','cancelled')),
        totals jsonb, error text, cost_usd numeric(12,6) NOT NULL DEFAULT 0, items_total int NOT NULL DEFAULT 0,
        items_done int NOT NULL DEFAULT 0, cancel_requested boolean NOT NULL DEFAULT false, attempts int NOT NULL DEFAULT 0,
        lease_owner text, lease_expires_at timestamptz, {BY}, finished_at timestamptz""",
    "experiment_results": f"""{ID}, {ORG}, experiment_id uuid NOT NULL REFERENCES experiments(id) ON DELETE CASCADE,
        item_id uuid NOT NULL REFERENCES dataset_items(id) ON DELETE CASCADE, output jsonb, output_hash text,
        latency_ms double precision, cost_usd numeric(12,6), error text, scores jsonb NOT NULL DEFAULT '{{}}',
        created_at timestamptz NOT NULL DEFAULT now(), UNIQUE (experiment_id, item_id)""",
    "human_labels": f"""{ID}, {ORG}, evaluator_id uuid NOT NULL REFERENCES evaluators(id) ON DELETE CASCADE,
        item_id uuid NOT NULL REFERENCES dataset_items(id) ON DELETE CASCADE, output_hash text NOT NULL,
        score int NOT NULL CHECK (score BETWEEN 1 AND 5), labelled_by uuid REFERENCES users(id) ON DELETE SET NULL,
        created_at timestamptz NOT NULL DEFAULT now(), UNIQUE (evaluator_id, item_id, output_hash, labelled_by)""",
    "release_requests": f"""{ID}, {ORG}, prompt_id uuid NOT NULL REFERENCES prompts(id) ON DELETE CASCADE, from_version int,
        to_version int NOT NULL, experiment_id uuid REFERENCES experiments(id) ON DELETE SET NULL, gate jsonb NOT NULL DEFAULT '{{}}',
        status text NOT NULL CHECK (status IN ('pending','approved','rejected','blocked')), reason text,
        requested_by uuid REFERENCES users(id) ON DELETE SET NULL, decided_by uuid REFERENCES users(id) ON DELETE SET NULL,
        created_at timestamptz NOT NULL DEFAULT now(), decided_at timestamptz""",
}
SYSTEM = {"experiments"}  # workers claim experiments across organisations


def upgrade() -> None:
    for name, columns in TABLES.items():
        op.execute(f"CREATE TABLE {name} ({columns})")
        enable_rls(name, system_access=name in SYSTEM)
    op.execute("CREATE INDEX dataset_items_dataset_idx ON dataset_items (dataset_id, seq)")
    op.execute("CREATE INDEX experiments_claim_idx ON experiments (created_at) WHERE status IN ('queued', 'running')")
    op.execute("CREATE INDEX experiment_results_exp_idx ON experiment_results (experiment_id)")
    op.execute("CREATE INDEX release_requests_prompt_idx ON release_requests (prompt_id, created_at DESC)")


def downgrade() -> None:
    for name in reversed(list(TABLES)):
        op.drop_table(name)
