"""Tests for ChargingProfileScheduler."""

import time
import pytest
from unittest.mock import patch

from src.charging_profile import ChargingProfileScheduler


def _make_profile(
    kind="Relative",
    rate_unit="A",
    periods=None,
    stack_level=0,
    purpose="TxProfile",
    start_schedule=None,
):
    """Build a charging profile dict (camelCase, as received from CSMS)."""
    if periods is None:
        periods = [{"startPeriod": 0, "limit": 32.0}]
    profile = {
        "chargingProfileId": stack_level + (100 if purpose != "TxProfile" else 0),
        "stackLevel": stack_level,
        "chargingProfilePurpose": purpose,
        "chargingProfileKind": kind,
        "chargingSchedule": {
            "chargingRateUnit": rate_unit,
            "chargingSchedulePeriod": periods,
        },
    }
    if start_schedule:
        profile["chargingSchedule"]["startSchedule"] = start_schedule
    return profile


class TestBasicProfile:
    def test_no_profile_returns_none(self):
        sched = ChargingProfileScheduler()
        assert sched.get_current_limit_kw() is None

    def test_has_profile_false_when_empty(self):
        sched = ChargingProfileScheduler()
        assert sched.has_profile is False

    def test_has_profile_true_after_set(self):
        sched = ChargingProfileScheduler()
        sched.set_profile(_make_profile())
        assert sched.has_profile is True

    def test_single_period_relative_amps(self):
        """Typical profile: Relative, 32A, single period."""
        sched = ChargingProfileScheduler()
        sched.set_profile(
            _make_profile(
                kind="Relative",
                rate_unit="A",
                periods=[{"startPeriod": 0, "limit": 32.0}],
            )
        )
        limit = sched.get_current_limit_kw()
        assert limit is not None
        # 32A * 230V = 7360W = 7.36 kW
        assert abs(limit - 7.36) < 0.01

    def test_single_period_watts(self):
        sched = ChargingProfileScheduler()
        sched.set_profile(
            _make_profile(
                kind="Relative",
                rate_unit="W",
                periods=[{"startPeriod": 0, "limit": 7400.0}],
            )
        )
        limit = sched.get_current_limit_kw()
        assert limit is not None
        assert abs(limit - 7.4) < 0.01

    def test_zero_limit_returns_zero(self):
        """A profile with limit=0 means pause charging."""
        sched = ChargingProfileScheduler()
        sched.set_profile(
            _make_profile(
                kind="Relative",
                rate_unit="A",
                periods=[{"startPeriod": 0, "limit": 0.0}],
            )
        )
        limit = sched.get_current_limit_kw()
        assert limit == 0.0


class TestMultiPeriod:
    def test_multi_period_selects_correct_one(self):
        """With multiple periods, the one whose startPeriod <= elapsed is active."""
        sched = ChargingProfileScheduler()
        sched.set_profile(
            _make_profile(
                kind="Relative",
                rate_unit="A",
                periods=[
                    {"startPeriod": 0, "limit": 32.0},
                    {"startPeriod": 3600, "limit": 0.0},  # pause after 1 hour
                    {"startPeriod": 7200, "limit": 16.0},  # resume at 16A after 2 hours
                ],
            )
        )

        # Just set, elapsed ~0s -> first period (32A)
        limit = sched.get_current_limit_kw()
        assert limit is not None
        assert abs(limit - 7.36) < 0.01

    def test_second_period_after_elapsed(self):
        """After enough time, the second period becomes active."""
        sched = ChargingProfileScheduler()
        sched.set_profile(
            _make_profile(
                kind="Relative",
                rate_unit="A",
                periods=[
                    {"startPeriod": 0, "limit": 32.0},
                    {"startPeriod": 10, "limit": 0.0},
                ],
            )
        )

        # Simulate 15 seconds passing
        sched._profile_start_times[("TxProfile", 1, 0)] = time.monotonic() - 15
        limit = sched.get_current_limit_kw()
        assert limit == 0.0


class TestSnakeCaseKeys:
    def test_snake_case_profile(self):
        """The ocpp library may deliver keys in snake_case."""
        profile = {
            "charging_profile_id": 1,
            "stack_level": 0,
            "charging_profile_purpose": "TxProfile",
            "charging_profile_kind": "Relative",
            "charging_schedule": {
                "charging_rate_unit": "A",
                "charging_schedule_period": [{"start_period": 0, "limit": 16.0}],
            },
        }
        sched = ChargingProfileScheduler()
        sched.set_profile(profile)
        limit = sched.get_current_limit_kw()
        assert limit is not None
        # 16A * 230V = 3680W = 3.68 kW
        assert abs(limit - 3.68) < 0.01


class TestClearProfile:
    def test_clear_all(self):
        sched = ChargingProfileScheduler()
        sched.set_profile(_make_profile(stack_level=0))
        sched.set_profile(_make_profile(stack_level=1))
        sched.clear_profile()
        assert sched.has_profile is False

    def test_clear_by_stack_level(self):
        sched = ChargingProfileScheduler()
        sched.set_profile(_make_profile(stack_level=0))
        sched.set_profile(_make_profile(stack_level=1))
        sched.clear_profile(stack_level=0)
        assert sched.has_profile is True
        assert ("TxProfile", 1, 0) not in sched._profiles
        assert ("TxProfile", 1, 1) in sched._profiles

    def test_clear_by_purpose(self):
        sched = ChargingProfileScheduler()
        sched.set_profile(_make_profile(purpose="TxProfile", stack_level=0))
        sched.set_profile(
            _make_profile(purpose="ChargePointMaxProfile", stack_level=1),
            connector_id=0,
        )
        sched.clear_profile(purpose="TxProfile")
        assert len(sched._profiles) == 1


class TestStackLevelPriority:
    def test_higher_stack_level_wins(self):
        sched = ChargingProfileScheduler()
        sched.set_profile(
            _make_profile(
                stack_level=0,
                rate_unit="A",
                periods=[{"startPeriod": 0, "limit": 32.0}],
            )
        )
        sched.set_profile(
            _make_profile(
                stack_level=1,
                rate_unit="A",
                periods=[{"startPeriod": 0, "limit": 16.0}],
            )
        )
        limit = sched.get_current_limit_kw()
        # Stack level 1 (16A) should win over stack level 0 (32A)
        assert abs(limit - 3.68) < 0.01


class TestProfileSummary:
    def test_no_profiles(self):
        sched = ChargingProfileScheduler()
        assert sched.profile_summary == "No active profiles"

    def test_with_profile(self):
        sched = ChargingProfileScheduler()
        sched.set_profile(_make_profile())
        summary = sched.profile_summary
        assert "1 periods" in summary
        assert "Relative" in summary


def test_profile_duration_expires():
    sched = ChargingProfileScheduler()
    profile = _make_profile()
    profile["chargingSchedule"]["duration"] = 10
    sched.set_profile(profile)
    for key in sched._profile_start_times:
        sched._profile_start_times[key] -= 15
    assert sched.get_current_limit_kw() is None


@pytest.mark.parametrize("field,offset", [("validFrom", 86400), ("validTo", -86400)])
def test_profile_validity_bounds(field, offset):
    from datetime import datetime, timezone, timedelta

    sched = ChargingProfileScheduler()
    profile = _make_profile()
    profile[field] = (
        datetime.now(timezone.utc) + timedelta(seconds=offset)
    ).isoformat()
    sched.set_profile(profile)
    assert sched.get_current_limit_kw() is None


def test_max_profile_remains_a_cap_across_different_purposes():
    sched = ChargingProfileScheduler()
    maximum = _make_profile(
        purpose="ChargePointMaxProfile",
        stack_level=0,
        rate_unit="W",
        periods=[{"startPeriod": 0, "limit": 2000}],
    )
    maximum["chargingProfileId"] = 10
    sched.set_profile(maximum, connector_id=0)
    transaction = _make_profile(
        stack_level=3, rate_unit="W", periods=[{"startPeriod": 0, "limit": 7000}]
    )
    sched.set_profile(transaction)
    assert sched.get_current_limit_kw() == 2


def test_clear_filters_are_combined_and_connector_scoped():
    sched = ChargingProfileScheduler()
    sched.set_profile(_make_profile(stack_level=0))
    other = _make_profile(stack_level=1, purpose="TxDefaultProfile")
    other["chargingProfileId"] = 11
    sched.set_profile(other)
    assert not sched.clear_profile(connector_id=2)
    assert not sched.clear_profile(stack_level=0, purpose="TxDefaultProfile")
    assert len(sched._profiles) == 2
    assert sched.clear_profile(stack_level=1, purpose="TxDefaultProfile")
    assert len(sched._profiles) == 1


def test_unsupported_recurring_profile_is_not_silently_accepted():
    sched = ChargingProfileScheduler()
    with pytest.raises(ValueError):
        sched.set_profile(_make_profile(kind="Recurring"))
