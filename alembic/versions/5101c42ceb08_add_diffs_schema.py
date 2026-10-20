"""add diffs schema

Revision ID: 5101c42ceb08
Revises: cc1758613c8f
Create Date: 2026-10-13 17:50:33.330375

"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '5101c42ceb08'
down_revision: str | Sequence[str] | None = 'cc1758613c8f'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    #JSON List -> One per file in diff-> includes filename, status, line counts, patch
    #JSON List is Paginated -> Each page holds a set number of files. Default is 30
    #idempotency key -> repo + PR Number + head SHA + base SHA

    #Two tables:
    #1) diffs: one row per version of the PR. Holds repo, pr number, head sha, base sha. unique id = repo + PR Number + head SHA + base SHA
    #2) diff_files one row per file, with a foreign key to diffs. Holds filename, status, additions, deletions, patch.
    #Tradeoff -> patch may sometimes be NULL, this is for very large files. in the pr comment, mention this
    op.execute("""
        CREATE TABLE diffs (
            id BIGSERIAL PRIMARY KEY,
            event_id BIGINT REFERENCES webhook_events(id) ON DELETE CASCADE,
            repo TEXT NOT NULL,
            pr_number INTEGER NOT NULL,
            base_sha TEXT NOT NULL,
            head_sha TEXT NOT NULL,
            fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (repo, pr_number, head_sha, base_sha)
        );
        CREATE TABLE diff_files(
            id BIGSERIAL PRIMARY KEY,
            diffref_id BIGINT NOT NULL REFERENCES diffs(id) ON DELETE CASCADE,
            filename TEXT NOT NULL,
            status TEXT NOT NULL,
            additions INTEGER NOT NULL,
            deletions INTEGER NOT NULL,
            changes INTEGER NOT NULL,
            patch TEXT,
            UNIQUE (diffref_id, filename)
        );
    """)



def downgrade() -> None:
    """Downgrade schema."""
    op.execute("""
               DROP TABLE diff_files;
               DROP TABLE diffs;
               """)


