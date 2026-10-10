DEFAULT_RECENT_LIMIT = 4
MAX_RECENT_LIMIT = 20


def validate_recent_limit(limit: int) -> None:
    if type(limit) is not int or not 1 <= limit <= MAX_RECENT_LIMIT:
        raise ValueError("limit must be between 1 and 20")
