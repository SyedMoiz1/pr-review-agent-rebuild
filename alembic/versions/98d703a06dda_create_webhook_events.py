"""create webhook events

Revision ID: 98d703a06dda
Revises: 
Create Date: 2026-10-05 19:04:08.296279

"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '98d703a06dda'
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("""
        CREATE TABLE webhook_events (
        id BIGSERIAL PRIMARY KEY,
        delivery_id TEXT UNIQUE NOT NULL,
        event_type TEXT,
        action TEXT,
        repo TEXT,
        pr_number INTEGER,
        payload JSONB NOT NULL,
        received_at TIMESTAMPTZ NOT NULL DEFAULT now() 
        )
    """)

def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP TABLE webhook_events")
    
