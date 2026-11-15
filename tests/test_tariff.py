"""Tariff evaluation from CDR plan detail."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from custom_components.openfeed import tariff

BNE = ZoneInfo("Australia/Brisbane")
SYD = ZoneInfo("Australia/Sydney")

TOU = {
    "fuelType": "ELECTRICITY",
    "tariffPeriod": [
        {
            "type": "RETAIL_SERVICE",
            "displayName": "All year",
            "startDate": "01-01",
            "endDate": "12-31",
            "dailySupplyCharges": "1.00",
            "rateBlockUType": "timeOfUseRates",
            "timeOfUseRates": [
                {
                    "type": "PEAK",
                    "displayName": "Peak",
                    "rates": [{"unitPrice": "0.40"}],
                    "timeOfUse": [{"days": ["MON", "TUE", "WED", "THU", "FRI"],
                                   "startTime": "16:00", "endTime": "21:00"}],
                },
                {
                    "type": "OFF_PEAK",
                    "displayName": "Overnight",
                    "rates": [{"unitPrice": "0.20"}],
                    "timeOfUse": [{"startTime": "21:00", "endTime": "07:00"}],
                },
                {
                    "type": "SHOULDER",
                    "rates": [{"unitPrice": "0.30", "measureUnit": "KWH"}],
                    "timeOfUse": [{"startTime": "07:00", "endTime": "21:00"}],
                },
            ],
        }
    ],
    "solarFeedInTariff": [
        {
            "displayName": "Retailer FiT",
            "payerType": "RETAILER",
            "tariffUType": "timeVaryingTariffs",
            "timeVaryingTariffs": [
                {"type": "PEAK", "amount": "0.10",
                 "timeVariations": [{"days": ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"],
                                     "startTime": "16:00", "endTime": "21:00"}]},
                {"amount": "0.02", "timeVariations": [{"days": ["MON", "TUE", "WED", "THU", "FRI",
                                                                "SAT", "SUN"]}]},
            ],
        },
        {"displayName": "Premium", "payerType": "GOVERNMENT", "tariffUType": "singleTariff",
         "singleTariff": {"amount": "0.44"}},
    ],
}


def _at(day: int, hour: int, minute: int = 0, tz=BNE) -> datetime:
    # 16 Nov 2026 is a Monday.
    return datetime(2026, 11, day, hour, minute, tzinfo=tz)


def test_peak_weekday() -> None:
    now = tariff.evaluate(TOU, _at(16, 17), BNE)
    assert now.import_type == "PEAK"
    assert now.import_ex_gst == 0.40
    assert tariff.inc_gst(now.import_ex_gst) == 0.44
    assert now.supply_ex_gst == 1.0
    # Peak feed-in plus the premium scheme, both paid.
    assert now.feed_in == round(0.10 + 0.44, 6)


def test_weekend_evening_is_shoulder_not_peak() -> None:
    now = tariff.evaluate(TOU, _at(21, 17), BNE)  # Saturday
    assert now.import_type == "SHOULDER"
    assert now.import_ex_gst == 0.30


def test_overnight_window_crosses_midnight() -> None:
    assert tariff.evaluate(TOU, _at(17, 2), BNE).import_type == "OFF_PEAK"
    assert tariff.evaluate(TOU, _at(16, 23), BNE).import_type == "OFF_PEAK"
    assert tariff.evaluate(TOU, _at(17, 7), BNE).import_type == "SHOULDER"


def test_explicit_offset_and_aest_zone() -> None:
    detail = {
        "tariffPeriod": [
            {"rateBlockUType": "timeOfUseRates", "timeZone": "AEST",
             "timeOfUseRates": [
                 {"type": "PEAK", "rates": [{"unitPrice": "0.5"}],
                  "timeOfUse": [{"startTime": "17:00", "endTime": "18:00"}]}]}
        ]
    }
    # 17:30 AEST is 18:30 in Sydney daylight time; AEST still applies.
    assert tariff.evaluate(detail, _at(16, 18, 30, SYD), SYD).import_type == "PEAK"
    detail["tariffPeriod"][0]["timeZone"] = "LOCAL"
    assert tariff.evaluate(detail, _at(16, 18, 30, SYD), SYD).import_type is None
    detail["tariffPeriod"][0]["timeOfUseRates"][0]["timeOfUse"][0].update(
        {"startTime": "17:00:00.000+10:00", "endTime": "18:00:00.000+10:00"}
    )
    assert tariff.evaluate(detail, _at(16, 18, 30, SYD), SYD).import_type == "PEAK"


def test_seasons_and_single_rate_blocks() -> None:
    detail = {
        "tariffPeriod": [
            {"displayName": "Summer", "startDate": "11-01", "endDate": "03-31",
             "dailySupplyCharges": "1.10", "rateBlockUType": "singleRate",
             "singleRate": {"displayName": "Anytime",
                            "rates": [{"unitPrice": "0.25", "volume": 10},
                                      {"unitPrice": "0.30"}]}},
            {"displayName": "Winter", "startDate": "04-01", "endDate": "10-31",
             "dailySupplyCharges": "0.90", "rateBlockUType": "singleRate",
             "singleRate": {"rates": [{"unitPrice": "0.20"}]}},
        ]
    }
    summer = tariff.evaluate(detail, _at(16, 12), BNE)
    assert summer.import_type == tariff.SINGLE_RATE
    assert summer.import_ex_gst == 0.25
    assert summer.periods == ["Summer"]
    assert len(summer.import_blocks) == 2
    assert summer.supply_ex_gst == 1.10
    winter = tariff.evaluate(detail, datetime(2026, 6, 1, 12, tzinfo=BNE), BNE)
    assert winter.import_ex_gst == 0.20 and winter.supply_ex_gst == 0.90


def test_network_and_retail_components_add() -> None:
    detail = {
        "tariffPeriod": [
            {"type": "NETWORK", "dailySupplyCharges": "0.60",
             "singleRate": {"rates": [{"unitPrice": "0.10"}]}},
            {"type": "RETAIL_SERVICE", "dailySupplyCharges": "0.40",
             "singleRate": {"rates": [{"unitPrice": "0.15"}]}},
        ]
    }
    now = tariff.evaluate(detail, _at(16, 12), BNE)
    assert now.import_ex_gst == 0.25
    assert now.supply_ex_gst == 1.0
    assert [c["type"] for c in now.components] == ["NETWORK", "RETAIL_SERVICE"]


def test_demand_charge() -> None:
    detail = {
        "tariffPeriod": [
            {"rateBlockUType": "demandCharges",
             "demandCharges": [{"displayName": "Peak demand", "amount": "0.20",
                                "measureUnit": "KW", "days": ["MON", "TUE", "WED", "THU", "FRI"],
                                "startTime": "16:00", "endTime": "21:00",
                                "measurementPeriod": "MONTH", "chargePeriod": "DAY"}]}
        ]
    }
    assert tariff.has_demand(detail)
    now = tariff.evaluate(detail, _at(16, 17), BNE)
    assert now.demand_ex_gst == 0.20 and now.demand_unit == "KW"
    assert now.demand_detail["measurement_period"] == "MONTH"
    assert tariff.evaluate(detail, _at(16, 12), BNE).demand_ex_gst is None


def test_next_change() -> None:
    change = tariff.next_change(TOU, _at(16, 12, 3), BNE)
    assert change == _at(16, 16)
    change = tariff.next_change(TOU, _at(16, 22), BNE)
    assert change == _at(17, 7)


def test_no_detail() -> None:
    assert tariff.evaluate(None, _at(16, 12), BNE) is None
    assert tariff.evaluate({"fees": []}, _at(16, 12), BNE) is None
    assert tariff.next_change(None, _at(16, 12), BNE) is None
    assert not tariff.has_import(None) and not tariff.has_feed_in({})


def test_window_helpers() -> None:
    utc = timezone.utc
    now = datetime(2026, 11, 16, 6, 30, tzinfo=utc)  # 16:30 Brisbane, Monday
    assert tariff.window_matches(["MON"], "16:00", "17:00", now, BNE)
    assert not tariff.window_matches(["TUE"], "16:00", "17:00", now, BNE)
    assert tariff.window_matches(None, None, None, now, BNE)
    assert not tariff.window_matches(["PUBLIC_HOLIDAYS"], "16:00", "17:00", now, BNE)
    assert tariff.window_matches(["MON", "PUBLIC_HOLIDAYS"], "16:00", "17:00", now, BNE)
    assert tariff.season_matches({}, now.date())
    assert tariff.season_matches({"startDate": "12-01", "endDate": "02-28"},
                                 (now + timedelta(days=40)).date())
