"""Monitor rules, the alerts they raise, online evaluators and the scores they write."""

from alembic import op

from app.rls import enable_rls

revision = "0014"
down_revision = "0013"


def upgrade() -> None:
    op.execute("""
        CREATE TABLE monitor_rules (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            org_id uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
            project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            name text NOT NULL,
            metric text NOT NULL CHECK (metric IN ('error_rate', 'p95_latency_ms', 'cost_per_hour', 'score_avg')),
            evaluator_id uuid REFERENCES evaluators(id) ON DELETE CASCADE,
            filter jsonb NOT NULL DEFAULT '{}',
            comparison text NOT NULL CHECK (comparison IN ('<', '>')),
            threshold double precision NOT NULL,
            window_minutes int NOT NULL CHECK (window_minutes IN (5, 15, 30, 60, 180, 360, 1440)),
            min_calls int NOT NULL DEFAULT 20,
            status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'paused')),
            created_by uuid REFERENCES users(id) ON DELETE SET NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (project_id, name)
        )
    """)
    enable_rls("monitor_rules")
    op.execute("""
        CREATE TABLE monitor_alerts (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            org_id uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
            rule_id uuid NOT NULL REFERENCES monitor_rules(id) ON DELETE CASCADE,
            window_start timestamptz NOT NULL,
            window_end timestamptz NOT NULL,
            value double precision NOT NULL,
            calls int NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            acknowledged_by uuid REFERENCES users(id) ON DELETE SET NULL,
            acknowledged_at timestamptz,
            investigation_run_id uuid REFERENCES runs(id) ON DELETE SET NULL,
            UNIQUE (rule_id, window_start)
        )
    """)
    op.execute("CREATE INDEX monitor_alerts_org_idx ON monitor_alerts (org_id, created_at DESC)")
    enable_rls("monitor_alerts")
    op.execute("""
        CREATE TABLE online_evaluators (
            org_id uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
            project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            evaluator_id uuid NOT NULL REFERENCES evaluators(id) ON DELETE CASCADE,
            sample_rate double precision NOT NULL CHECK (sample_rate > 0 AND sample_rate <= 1),
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (project_id, evaluator_id)
        )
    """)
    enable_rls("online_evaluators")
    op.execute("""
        CREATE TABLE trace_scores (
            org_id uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
            project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            trace_id text NOT NULL,
            span_id text NOT NULL,
            evaluator_id uuid NOT NULL REFERENCES evaluators(id) ON DELETE CASCADE,
            value double precision NOT NULL,
            passed boolean NOT NULL,
            reason text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (project_id, trace_id, span_id, evaluator_id)
        )
    """)
    enable_rls("trace_scores")


def downgrade() -> None:
    for table in ("trace_scores", "online_evaluators", "monitor_alerts", "monitor_rules"):
        op.drop_table(table)
