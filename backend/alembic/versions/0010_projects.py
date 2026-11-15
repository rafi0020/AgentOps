"""Projects (one per observed application) and their ingestion keys."""

from alembic import op

from app.rls import enable_rls

revision = "0010"
down_revision = "0006"


def upgrade() -> None:
    op.execute("""
        CREATE TABLE projects (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            org_id uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
            name text NOT NULL,
            retention_days int NOT NULL DEFAULT 30 CHECK (retention_days BETWEEN 1 AND 3650),
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (org_id, name)
        )
    """)
    # Retention has to know the longest period any project keeps data, across organisations, before it may
    # remove a whole day's partition. That is a system operation, like finding a key by its hash.
    enable_rls("projects", system_access=True)
    op.execute("""
        CREATE TABLE ingest_keys (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            org_id uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
            project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            name text NOT NULL,
            key_hash text NOT NULL UNIQUE,
            prefix text NOT NULL,
            scopes text[] NOT NULL DEFAULT '{ingest}',
            created_by uuid REFERENCES users(id) ON DELETE SET NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            revoked_at timestamptz,
            last_used_at timestamptz
        )
    """)
    op.execute("CREATE INDEX ingest_keys_project_idx ON ingest_keys (project_id)")
    # A key arrives before anything is known about its organisation, so looking one up by its hash is a
    # system operation, like finding a user at sign-in.
    enable_rls("ingest_keys", system_access=True)
    op.execute("""
        CREATE TABLE ingest_batches (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            org_id uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
            project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            payload bytea NOT NULL,
            content_type text NOT NULL,
            span_count int NOT NULL,
            status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'processing', 'dead')),
            attempts int NOT NULL DEFAULT 0,
            lease_owner text,
            lease_expires_at timestamptz,
            error text,
            received_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX ingest_batches_claim_idx ON ingest_batches (received_at) WHERE status <> 'dead'")
    op.execute("CREATE INDEX ingest_batches_dead_idx ON ingest_batches (org_id, received_at) WHERE status = 'dead'")
    enable_rls("ingest_batches", system_access=True)  # ingest workers claim across organisations


def downgrade() -> None:
    for table in ("ingest_batches", "ingest_keys", "projects"):
        op.drop_table(table)
