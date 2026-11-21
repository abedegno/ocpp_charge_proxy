"""Single-phase Absolute/Relative OCPP profile evaluation for the emulator."""

from __future__ import annotations

from datetime import datetime, timezone
import math
import time


def _get(data, *keys):
    for key in keys:
        if key in data:
            return data[key]
    return None


def _timestamp(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Profile timestamps must have a timezone")
    return parsed


class ChargingProfileScheduler:
    def __init__(self, rated_power_w=7400.0):
        self._rated_power_w = rated_power_w
        self._profiles = {}  # (purpose, connector, stack level) -> profile
        self._profile_start_times = {}
        self._transaction_start_time = None

    def start_transaction(self):
        self._transaction_start_time = time.monotonic()

    def set_profile(self, profile, connector_id=1):
        schedule = _get(profile, "charging_schedule", "chargingSchedule") or {}
        kind = _get(profile, "charging_profile_kind", "chargingProfileKind")
        purpose = _get(profile, "charging_profile_purpose", "chargingProfilePurpose")
        unit = _get(schedule, "charging_rate_unit", "chargingRateUnit")
        periods = (
            _get(schedule, "charging_schedule_period", "chargingSchedulePeriod") or []
        )
        if (
            kind not in ("Absolute", "Relative")
            or unit not in ("A", "W")
            or not periods
        ):
            raise ValueError("Unsupported charging profile")
        if purpose not in ("ChargePointMaxProfile", "TxDefaultProfile", "TxProfile"):
            raise ValueError("Unsupported profile purpose")
        if connector_id not in (0, 1) or (
            purpose == "ChargePointMaxProfile" and connector_id != 0
        ):
            raise ValueError("Unsupported connector for profile")
        if purpose == "TxProfile" and connector_id != 1:
            raise ValueError("Transaction profiles require connector 1")
        start = _get(schedule, "start_schedule", "startSchedule")
        if kind == "Absolute" and not start:
            raise ValueError("Absolute profile requires startSchedule")
        if start:
            _timestamp(start)
        for field in (("valid_from", "validFrom"), ("valid_to", "validTo")):
            value = _get(profile, *field)
            if value:
                _timestamp(value)
        duration = _get(schedule, "duration")
        if duration is not None and (type(duration) is not int or duration <= 0):
            raise ValueError("Invalid schedule duration")
        offsets = []
        for period in periods:
            limit = period.get("limit")
            offset = _get(period, "start_period", "startPeriod")
            phases = _get(period, "number_phases", "numberPhases")
            if (
                type(limit) not in (int, float)
                or not math.isfinite(limit)
                or limit < 0
                or type(offset) is not int
                or offset < 0
                or phases not in (None, 1)
            ):
                raise ValueError("Unsupported schedule period")
            offsets.append(offset)
        if offsets[0] != 0 or offsets != sorted(set(offsets)):
            raise ValueError("Schedule periods must start at zero and increase")
        profile_id = _get(profile, "charging_profile_id", "chargingProfileId")
        # A replacement ID must not leave an old profile active at another level.
        for key, old in list(self._profiles.items()):
            if _get(old, "charging_profile_id", "chargingProfileId") == profile_id:
                del self._profiles[key]
                self._profile_start_times.pop(key, None)
        level = _get(profile, "stack_level", "stackLevel") or 0
        key = (purpose, connector_id, level)
        self._profiles[key] = profile
        self._profile_start_times[key] = time.monotonic()

    def clear_profile(
        self, profile_id=None, connector_id=None, purpose=None, stack_level=None
    ):
        matched = []
        for key, profile in self._profiles.items():
            if profile_id is not None:
                match = (
                    _get(profile, "charging_profile_id", "chargingProfileId")
                    == profile_id
                )
            else:
                match = (
                    (connector_id is None or key[1] == connector_id)
                    and (purpose is None or key[0] == purpose)
                    and (stack_level is None or key[2] == stack_level)
                )
            if match:
                matched.append(key)
        for key in matched:
            del self._profiles[key]
            self._profile_start_times.pop(key, None)
        return bool(matched)

    def get_current_limit_kw(self):
        now_utc, now_mono = datetime.now(timezone.utc), time.monotonic()
        limits = {}
        # Connector-specific default profiles override chargepoint-wide defaults.
        for key in sorted(
            self._profiles, key=lambda key: (key[1], key[2]), reverse=True
        ):
            purpose, connector, _ = key
            if purpose in limits:
                continue
            value = self._evaluate_profile(self._profiles[key], now_utc, now_mono, key)
            if value is not None:
                limits[purpose] = value / 1000
        transaction = limits.get("TxProfile", limits.get("TxDefaultProfile"))
        maximum = limits.get("ChargePointMaxProfile")
        applicable = [value for value in (transaction, maximum) if value is not None]
        return min(applicable) if applicable else None

    def _evaluate_profile(self, profile, now_utc, now_mono, key):
        for fields, before in (
            (("valid_from", "validFrom"), True),
            (("valid_to", "validTo"), False),
        ):
            value = _get(profile, *fields)
            if value and (
                (now_utc < _timestamp(value))
                if before
                else (now_utc >= _timestamp(value))
            ):
                return None
        schedule = _get(profile, "charging_schedule", "chargingSchedule")
        if _get(profile, "charging_profile_kind", "chargingProfileKind") == "Relative":
            anchor = self._transaction_start_time
            if anchor is None:
                anchor = self._profile_start_times[key]
            elapsed = now_mono - anchor
        else:
            elapsed = (
                now_utc - _timestamp(_get(schedule, "start_schedule", "startSchedule"))
            ).total_seconds()
        duration = schedule.get("duration")
        if elapsed < 0 or (duration is not None and elapsed >= duration):
            return None
        active = None
        for period in _get(
            schedule, "charging_schedule_period", "chargingSchedulePeriod"
        ):
            if _get(period, "start_period", "startPeriod") <= elapsed:
                active = period
        if active is None:
            return None
        limit = float(active["limit"])
        return (
            limit * 230
            if _get(schedule, "charging_rate_unit", "chargingRateUnit") == "A"
            else limit
        )

    @property
    def has_profile(self):
        return bool(self._profiles)

    @property
    def profile_summary(self):
        if not self._profiles:
            return "No active profiles"
        lines = []
        for key, profile in sorted(self._profiles.items()):
            schedule = _get(profile, "charging_schedule", "chargingSchedule")
            periods = _get(
                schedule, "charging_schedule_period", "chargingSchedulePeriod"
            )
            lines.append(
                f"  Profile {_get(profile, 'charging_profile_id', 'chargingProfileId')}: "
                f"{len(periods)} periods, kind={_get(profile, 'charging_profile_kind', 'chargingProfileKind')}, "
                f"purpose={key[0]}, connector={key[1]}, stack={key[2]}"
            )
        return "\n".join(lines)
