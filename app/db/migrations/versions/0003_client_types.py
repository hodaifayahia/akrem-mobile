"""Add client-type color, ordering, and system flag to categories.

Revision ID: 0003_client_types
Revises: 0002_product_catalog
Create Date: 2026-10-01
"""

from alembic import op
import sqlalchemy as sa

revision = "0003_client_types"
down_revision = "0002_product_catalog"
branch_labels = None
depends_on = None

FALLBACK_TYPE_NAME = "غير مصنف"
DEFAULT_TYPE_STYLES: tuple[tuple[str, str, int], ...] = (
    ("أساتذة", "blue", 1),
    ("منحة البطالة", "amber", 2),
    ("عسكري", "green", 3),
    ("أعمال حرة", "violet", 4),
    (FALLBACK_TYPE_NAME, "slate", 99),
)
CUSTOM_SORT_START = 10

categories = sa.table(
    "categories",
    sa.column("id", sa.Integer()),
    sa.column("name", sa.String()),
    sa.column("color", sa.String()),
    sa.column("sort_order", sa.Integer()),
    sa.column("is_system", sa.Boolean()),
)


def upgrade() -> None:
    """Add the new columns, then style the default types and order custom ones."""
    # recreate="never" keeps SQLite on plain ALTER TABLE: a batch table copy would
    # DROP the referenced categories table and trip the customers RESTRICT foreign key.
    with op.batch_alter_table("categories", recreate="never") as batch_op:
        batch_op.add_column(
            sa.Column("color", sa.String(length=16), nullable=False, server_default="slate")
        )
        batch_op.add_column(
            sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(
            sa.Column("is_system", sa.Boolean(), nullable=False, server_default=sa.false())
        )
    _style_default_types()
    _order_custom_types()


def _style_default_types() -> None:
    """Assign the documented colors/order to the default types and protect the fallback."""
    for name, color, sort_order in DEFAULT_TYPE_STYLES:
        op.execute(
            categories.update()
            .where(categories.c.name == name)
            .values(color=color, sort_order=sort_order, is_system=name == FALLBACK_TYPE_NAME)
        )


def _order_custom_types() -> None:
    """Give owner-created types sort orders 10, 11, ... in creation (id) order."""
    default_names = [name for name, _color, _order in DEFAULT_TYPE_STYLES]
    connection = op.get_bind()
    custom_ids = connection.execute(
        sa.select(categories.c.id)
        .where(categories.c.name.not_in(default_names))
        .order_by(categories.c.id)
    ).scalars().all()
    for offset, category_id in enumerate(custom_ids):
        connection.execute(
            categories.update()
            .where(categories.c.id == category_id)
            .values(sort_order=CUSTOM_SORT_START + offset)
        )


def downgrade() -> None:
    """Drop the client-type columns; names and customer links are untouched."""
    # Plain ALTER TABLE DROP COLUMN (SQLite 3.35+) for the same foreign-key reason.
    with op.batch_alter_table("categories", recreate="never") as batch_op:
        batch_op.drop_column("is_system")
        batch_op.drop_column("sort_order")
        batch_op.drop_column("color")
