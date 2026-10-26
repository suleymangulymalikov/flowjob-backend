"""baseline (empty schema)

BE-002 only sets up the migration tooling, so this revision creates no tables.
The real schema (users, profiles, CVs, applications, status history, Gmail data)
arrives with the ERD and feature tasks, each as its own revision on top of this one.

Revision ID: 0001
Revises: 
Create Date: 2026-10-26 16:25:47.488101

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0001'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
