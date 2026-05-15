from datetime import date, timedelta


def current_sunday(today: date | None = None) -> date:
    """Most-recent Sunday on or before ``today``. ``today`` injectable for tests."""
    today = today or date.today()
    return today - timedelta(days=(today.weekday() + 1) % 7)


def next_sunday(today: date | None = None) -> date:
    """Sunday of next week relative to ``today``."""
    return current_sunday(today) + timedelta(days=7)


def is_sunday(iso: str) -> bool:
    return date.fromisoformat(iso).weekday() == 6


def is_week_halfway_over(today: date | None = None) -> bool:
    """True when today is Thursday, Friday, or Saturday — past the Sun–Sat midpoint."""
    today = today or date.today()
    return today.weekday() in {3, 4, 5}
