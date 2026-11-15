"""Five-minute roll-ups that every chart, monitor and agent tool reads instead of raw spans."""

from alembic import op

from app.rls import enable_rls

revision = "0012"
down_revision = "0011"


def upgrade() -> None:
    # Latency is kept as a histogram with fixed bounds, so buckets can be added together and a percentile
    # read from the sum. These two make that addition available in SQL.
    op.execute("""
        CREATE FUNCTION int_array_add(a int[], b int[]) RETURNS int[] LANGUAGE sql IMMUTABLE AS $$
            SELECT CASE WHEN a IS NULL THEN b WHEN b IS NULL THEN a
                   ELSE ARRAY(SELECT coalesce(a[i], 0) + coalesce(b[i], 0)
                              FROM generate_series(1, greatest(cardinality(a), cardinality(b))) AS i) END
        $$
    """)
    op.execute("CREATE AGGREGATE sum_int_array(int[]) (SFUNC = int_array_add, STYPE = int[])")
    op.execute("GRANT EXECUTE ON FUNCTION int_array_add(int[], int[]), sum_int_array(int[]) TO agentops_app")
    op.execute("""
        CREATE TABLE rollups_5m (
            org_id uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
            project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            bucket timestamptz NOT NULL,
            model text NOT NULL DEFAULT '',
            operation text NOT NULL DEFAULT '',
            tool text NOT NULL DEFAULT '',
            prompt_name text NOT NULL DEFAULT '',
            prompt_version int NOT NULL DEFAULT 0,
            service_version text NOT NULL DEFAULT '',
            calls int NOT NULL DEFAULT 0,
            errors int NOT NULL DEFAULT 0,
            roots int NOT NULL DEFAULT 0,
            input_tokens bigint NOT NULL DEFAULT 0,
            output_tokens bigint NOT NULL DEFAULT 0,
            cost_usd numeric(16,8) NOT NULL DEFAULT 0,
            latency_hist int[] NOT NULL,
            PRIMARY KEY (project_id, bucket, model, operation, tool, prompt_name, prompt_version, service_version)
        )
    """)
    enable_rls("rollups_5m")
    op.execute("""
        CREATE TABLE score_rollups_5m (
            org_id uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
            project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            bucket timestamptz NOT NULL,
            evaluator_id uuid NOT NULL,
            count int NOT NULL DEFAULT 0,
            sum double precision NOT NULL DEFAULT 0,
            PRIMARY KEY (project_id, bucket, evaluator_id)
        )
    """)
    enable_rls("score_rollups_5m")
    # Lists the days that have their own partition, for the retention job.
    op.execute("""
        CREATE FUNCTION span_partition_days() RETURNS SETOF date LANGUAGE sql SECURITY DEFINER SET search_path = public AS
        $$ SELECT to_date(right(c.relname, 8), 'YYYYMMDD') FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid
           WHERE i.inhparent = 'spans'::regclass AND c.relname ~ '^spans_[0-9]{8}$' $$
    """)
    op.execute("REVOKE ALL ON FUNCTION span_partition_days() FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION span_partition_days() TO agentops_app")


def downgrade() -> None:
    op.execute("DROP FUNCTION span_partition_days()")
    op.drop_table("score_rollups_5m")
    op.drop_table("rollups_5m")
    op.execute("DROP AGGREGATE sum_int_array(int[])")
    op.execute("DROP FUNCTION int_array_add(int[], int[])")
