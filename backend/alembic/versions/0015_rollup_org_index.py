"""Roll-ups indexed by organisation and time, for reads across all of an organisation's projects.

Found by the scale test: the primary keys lead with project_id, so the Overview with no project chosen read
every organisation's roll-ups and kept the rows of one."""

from alembic import op

revision = "0015"
down_revision = "0014"


def upgrade() -> None:
    op.execute("CREATE INDEX rollups_5m_org_bucket_idx ON rollups_5m (org_id, bucket)")
    op.execute("CREATE INDEX score_rollups_5m_org_bucket_idx ON score_rollups_5m (org_id, bucket)")


def downgrade() -> None:
    op.execute("DROP INDEX score_rollups_5m_org_bucket_idx")
    op.execute("DROP INDEX rollups_5m_org_bucket_idx")
