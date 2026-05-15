from datetime import date, timedelta

import pytest

from pantry_cooking_vibes.dates import (
    current_sunday,
    is_week_halfway_over,
    next_sunday,
)

# Anchored to known values verified by test output:
# 2026-05-10 is a Sunday (weekday=6); 2026-05-04 is a Monday (weekday=0).
_SUN = date(2026, 5, 10)  # weekday 6
_MON = date(2026, 5, 4)  # weekday 0
_WED = date(2026, 5, 6)  # weekday 2
_THU = date(2026, 5, 7)  # weekday 3
_FRI = date(2026, 5, 8)  # weekday 4
_SAT = date(2026, 5, 9)  # weekday 5


@pytest.mark.parametrize("today", [_SUN, _MON, _WED, _THU, _FRI, _SAT])
def test_next_sunday_is_seven_days_after_current(today):
    assert next_sunday(today) == current_sunday(today) + timedelta(days=7)


def test_next_sunday_from_sunday():
    assert next_sunday(_SUN) == date(2026, 5, 17)


def test_next_sunday_from_midweek():
    assert next_sunday(_WED) == date(2026, 5, 10)


@pytest.mark.parametrize("today", [_THU, _FRI, _SAT])
def test_is_week_halfway_over_true(today):
    assert is_week_halfway_over(today) is True


@pytest.mark.parametrize("today", [_SUN, _MON, _WED])
def test_is_week_halfway_over_false(today):
    assert is_week_halfway_over(today) is False
