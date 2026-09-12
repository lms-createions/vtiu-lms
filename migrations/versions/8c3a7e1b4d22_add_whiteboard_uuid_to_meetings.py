"""Add Agora whiteboard room UUID to meetings.

Revision ID: 8c3a7e1b4d22
Revises: 0528bde5114b
"""
from alembic import op
import sqlalchemy as sa


revision = "8c3a7e1b4d22"
down_revision = "0528bde5114b"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    table_name = "meetings"
    if table_name not in inspector.get_table_names():
        return

    columns = {column["name"] for column in inspector.get_columns(table_name)}
    indexes = {index["name"] for index in inspector.get_indexes(table_name)}

    if connection.dialect.name == "postgresql":
        if "whiteboard_uuid" not in columns:
            op.execute(sa.text(
                "ALTER TABLE meetings ADD COLUMN IF NOT EXISTS "
                "whiteboard_uuid VARCHAR(120)"
            ))
        if "ix_meetings_whiteboard_uuid" not in indexes:
            op.execute(sa.text(
                "CREATE UNIQUE INDEX IF NOT EXISTS ix_meetings_whiteboard_uuid "
                "ON meetings (whiteboard_uuid)"
            ))
        return

    with op.batch_alter_table(table_name, schema=None) as batch_op:
        if "whiteboard_uuid" not in columns:
            batch_op.add_column(sa.Column("whiteboard_uuid", sa.String(length=120), nullable=True))
        if "ix_meetings_whiteboard_uuid" not in indexes:
            batch_op.create_index("ix_meetings_whiteboard_uuid", ["whiteboard_uuid"], unique=True)


def downgrade():
    with op.batch_alter_table("meetings", schema=None) as batch_op:
        batch_op.drop_index("ix_meetings_whiteboard_uuid")
        batch_op.drop_column("whiteboard_uuid")
