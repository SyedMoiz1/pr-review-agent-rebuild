"""update constraints

Revision ID: cc1758613c8f
Revises: 98d703a06dda
Create Date: 2026-10-05 19:58:06.948577

"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'cc1758613c8f'
down_revision: str | Sequence[str] | None = '98d703a06dda'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("""
        ALTER TABLE webhook_events
        ALTER COLUMN event_type SET NOT NULL,
        ALTER COLUMN action SET NOT NULL,
        ALTER COLUMN repo SET NOT NULL,
        ALTER COLUMN pr_number SET NOT NULL;
    """)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("""
        ALTER TABLE webhook_events
        ALTER COLUMN event_type DROP NOT NULL,
        ALTER COLUMN action DROP NOT NULL,
        ALTER COLUMN repo DROP NOT NULL,
        ALTER COLUMN pr_number DROP NOT NULL;
    """)
