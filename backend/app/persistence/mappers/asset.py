from uuid import UUID

from app.domain.asset import Asset, AssetType
from app.persistence.mappers.timestamps import datetime_to_epoch_us, epoch_us_to_datetime
from app.persistence.models.asset import AssetRow


def asset_to_row(asset: Asset) -> AssetRow:
    return AssetRow(
        asset_id=str(asset.asset_id),
        asset_type=asset.asset_type.value,
        relative_path=asset.relative_path,
        sha256=asset.sha256,
        size_bytes=asset.size_bytes,
        created_at_us=datetime_to_epoch_us(asset.created_at),
        verified_at_us=(
            datetime_to_epoch_us(asset.verified_at) if asset.verified_at is not None else None
        ),
    )


def asset_from_row(row: AssetRow) -> Asset:
    return Asset(
        asset_id=UUID(row.asset_id),
        asset_type=AssetType(row.asset_type),
        relative_path=row.relative_path,
        sha256=row.sha256,
        size_bytes=row.size_bytes,
        created_at=epoch_us_to_datetime(row.created_at_us),
        verified_at=(
            epoch_us_to_datetime(row.verified_at_us)
            if row.verified_at_us is not None
            else None
        ),
    )
