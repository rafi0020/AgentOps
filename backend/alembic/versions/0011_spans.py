"""Spans (partitioned by day), trace summaries and model prices."""

from alembic import op

from app.rls import enable_rls

revision = "0011"
down_revision = "0010"


def upgrade() -> None:
    op.execute("""
        CREATE TABLE spans (
            org_id uuid NOT NULL,
            project_id uuid NOT NULL,
            trace_id text NOT NULL,
            span_id text NOT NULL,
            parent_span_id text,
            name text NOT NULL,
            kind int NOT NULL DEFAULT 0,
            start_time timestamptz NOT NULL,
            end_time timestamptz NOT NULL,
            duration_ms double precision NOT NULL,
            status text NOT NULL,
            status_message text,
            operation text,
            model text,
            tool text,
            provider text,
            prompt_name text,
            prompt_version int,
            service_name text,
            service_version text,
            session_id text,
            input_tokens int,
            output_tokens int,
            cost_usd numeric(14,8),
            attributes jsonb NOT NULL DEFAULT '{}',
            content jsonb,
            start_day date NOT NULL,
            PRIMARY KEY (project_id, start_day, trace_id, span_id)
        ) PARTITION BY RANGE (start_day)
    """)
    op.execute("CREATE INDEX spans_trace_idx ON spans (project_id, trace_id)")
    op.execute("CREATE INDEX spans_time_idx ON spans (project_id, start_time)")
    op.execute("CREATE INDEX spans_error_idx ON spans (project_id, start_time) WHERE status = 'error'")
    op.execute("CREATE TABLE spans_default PARTITION OF spans DEFAULT")
    enable_rls("spans")

    # The application role is not the table owner and so cannot create or drop partitions. These two
    # functions run with the owner's rights and do exactly one thing each. A new partition is made
    # unreachable except through the parent table, where row-level security applies.
    op.execute("""
        CREATE FUNCTION ensure_span_partition(day date) RETURNS boolean
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
        DECLARE part text := 'spans_' || to_char(day, 'YYYYMMDD');
        BEGIN
            IF to_regclass('public.' || part) IS NOT NULL THEN RETURN false; END IF;
            EXECUTE format('CREATE TABLE %I PARTITION OF spans FOR VALUES FROM (%L) TO (%L)', part, day, day + 1);
            EXECUTE format('REVOKE ALL ON %I FROM agentops_app', part);
            RETURN true;
        EXCEPTION WHEN duplicate_table OR check_violation THEN
            RETURN false;  -- made by another worker, or rows for that day already sit in the default partition
        END $$
    """)
    op.execute("""
        CREATE FUNCTION drop_span_partition(day date) RETURNS boolean
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
        DECLARE part text := 'spans_' || to_char(day, 'YYYYMMDD');
        BEGIN
            IF to_regclass('public.' || part) IS NULL THEN RETURN false; END IF;
            EXECUTE format('DROP TABLE %I', part);
            RETURN true;
        END $$
    """)
    op.execute("REVOKE ALL ON FUNCTION ensure_span_partition(date), drop_span_partition(date) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION ensure_span_partition(date), drop_span_partition(date) TO agentops_app")
    op.execute("REVOKE ALL ON spans_default FROM agentops_app")

    op.execute("""
        CREATE TABLE traces (
            org_id uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
            project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            trace_id text NOT NULL,
            root_name text NOT NULL,
            status text NOT NULL,
            start_time timestamptz NOT NULL,
            end_time timestamptz NOT NULL,
            duration_ms double precision NOT NULL,
            span_count int NOT NULL,
            error_count int NOT NULL,
            input_tokens bigint NOT NULL DEFAULT 0,
            output_tokens bigint NOT NULL DEFAULT 0,
            cost_usd numeric(14,8) NOT NULL DEFAULT 0,
            models text[] NOT NULL DEFAULT '{}',
            tools text[] NOT NULL DEFAULT '{}',
            session_id text,
            service_name text,
            service_version text,
            PRIMARY KEY (project_id, trace_id)
        )
    """)
    op.execute("CREATE INDEX traces_time_idx ON traces (project_id, start_time DESC, trace_id)")
    op.execute("CREATE INDEX traces_error_idx ON traces (project_id, start_time DESC) WHERE status = 'error'")
    op.execute(
        "CREATE INDEX traces_session_idx ON traces (project_id, session_id, start_time) WHERE session_id IS NOT NULL"
    )
    enable_rls("traces")

    op.execute("""
        CREATE TABLE prices (
            org_id uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
            model text NOT NULL,
            input_per_mtok numeric(10,4) NOT NULL CHECK (input_per_mtok >= 0),
            output_per_mtok numeric(10,4) NOT NULL CHECK (output_per_mtok >= 0),
            updated_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (org_id, model)
        )
    """)
    enable_rls("prices")


def downgrade() -> None:
    op.drop_table("prices")
    op.drop_table("traces")
    op.execute("DROP FUNCTION ensure_span_partition(date), drop_span_partition(date)")
    op.drop_table("spans")
