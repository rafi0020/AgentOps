"""Experiment target headers are stored encrypted, as provider keys are; rows written before that are sealed here.

The key is derived from SECRET_KEY in Python, so the rows are rewritten here rather than in SQL. Each organisation
is handled in its own transaction context, because the experiments table is protected by row-level security.
"""

import json

from alembic import op
from sqlalchemy import text

revision = "0016"
down_revision = "0015"


def upgrade() -> None:
    from app.security.crypto import encrypt

    bind = op.get_bind()
    for org_id in bind.execute(text("SELECT id FROM orgs")).scalars().all():
        bind.execute(text("SELECT set_config('app.org_id', :org, true)"), {"org": str(org_id)})
        rows = bind.execute(
            text(
                "SELECT id, target FROM experiments WHERE jsonb_typeof(target->'headers') = 'object' "
                "AND target->'headers' <> '{}'::jsonb"
            )
        ).all()
        for row in rows:
            target = dict(row.target)
            headers = target.pop("headers")
            target["headers_enc"] = encrypt(json.dumps(headers, sort_keys=True))
            bind.execute(
                text("UPDATE experiments SET target = CAST(:target AS jsonb) WHERE id = :id"),
                {"target": json.dumps(target), "id": row.id},
            )


def downgrade() -> None:
    raise NotImplementedError("Headers are not decrypted back to plain text; restore from a backup if needed.")
