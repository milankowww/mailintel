# SPDX-FileCopyrightText: Milan Pikula
# SPDX-License-Identifier: EUPL-1.2

"""Track current records for incremental mobile synchronization."""
from pathlib import Path

from alembic import op

revision = "0002_sync_generations"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(Path(__file__).with_suffix(".sql").read_text(encoding="utf-8"))


def downgrade() -> None:
    raise RuntimeError("Restore a database backup to undo synchronization generations")
