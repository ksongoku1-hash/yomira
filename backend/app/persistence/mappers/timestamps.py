from datetime import UTC, datetime, timedelta

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
MICROSECONDS_PER_SECOND = 1_000_000
SECONDS_PER_DAY = 86_400


def datetime_to_epoch_us(value: datetime) -> int:
    delta = value.astimezone(UTC) - EPOCH
    return (
        (delta.days * SECONDS_PER_DAY + delta.seconds) * MICROSECONDS_PER_SECOND
        + delta.microseconds
    )


def epoch_us_to_datetime(value: int) -> datetime:
    return EPOCH + timedelta(microseconds=value)
