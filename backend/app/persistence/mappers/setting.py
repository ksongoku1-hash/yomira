from app.domain.setting import Setting
from app.persistence.mappers.timestamps import datetime_to_epoch_us, epoch_us_to_datetime
from app.persistence.models.setting import SettingRow


def setting_to_row(setting: Setting) -> SettingRow:
    return SettingRow(
        setting_key=setting.setting_key,
        value_json=setting.value_json,
        schema_version=setting.schema_version,
        updated_at_us=datetime_to_epoch_us(setting.updated_at),
    )


def setting_from_row(row: SettingRow) -> Setting:
    return Setting(
        setting_key=row.setting_key,
        value_json=row.value_json,
        schema_version=row.schema_version,
        updated_at=epoch_us_to_datetime(row.updated_at_us),
    )
