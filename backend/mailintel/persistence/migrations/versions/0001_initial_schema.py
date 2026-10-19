"""Initial MailIntel schema."""
from pathlib import Path

from alembic import op

revision = "0001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Frozen SQL: future model edits must not change an existing revision.
    sql = Path(__file__).with_suffix(".sql").read_text(encoding="utf-8")
    op.execute(sql)


def downgrade() -> None:
    raise RuntimeError("Restore a database backup to undo the initial schema")
