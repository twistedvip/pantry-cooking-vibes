from datetime import date, timedelta


def current_sunday(today: date | None = None) -> date:
    """Most-recent Sunday on or before ``today``. ``today`` injectable for tests."""
    today = today or date.today()
    return today - timedelta(days=(today.weekday() + 1) % 7)


def is_sunday(iso: str) -> bool:
    return date.fromisoformat(iso).weekday() == 6
