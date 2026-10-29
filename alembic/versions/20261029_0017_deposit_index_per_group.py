"""Each chain group derives from its own HD seed, so indexes are unique per group."""

from alembic import op
import sqlalchemy as sa

revision = "0017_deposit_index_per_group"
down_revision = "0016_liquidity_recognition"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint("deposit_addresses_derivation_index_key", "deposit_addresses", type_="unique")
    op.create_unique_constraint(
        "uq_deposit_address_group_index", "deposit_addresses", ["chain_group", "derivation_index"]
    )


def downgrade():
    if op.get_bind().execute(sa.text(
        "SELECT EXISTS(SELECT 1 FROM deposit_addresses GROUP BY derivation_index HAVING count(*) > 1)"
    )).scalar_one():
        raise RuntimeError("refusing to merge per-group derivation indexes that overlap")
    op.drop_constraint("uq_deposit_address_group_index", "deposit_addresses", type_="unique")
    op.create_unique_constraint(
        "deposit_addresses_derivation_index_key", "deposit_addresses", ["derivation_index"]
    )
