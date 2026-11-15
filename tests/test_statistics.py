"""Statistics helpers."""

from unittest.mock import MagicMock, patch

from custom_components.openfeed.statistics import async_clear


async def test_clear_does_not_wait_on_recorder() -> None:
    """Waiting on the recorder during bootstrap stalls HA startup (v0.1.5)."""
    instance = MagicMock()
    instance.async_block_till_done.side_effect = AssertionError("must not wait")
    with patch(
        "custom_components.openfeed.statistics.get_instance", return_value=instance
    ):
        await async_clear(MagicMock(), ["openfeed:acct_x_payments"])
    instance.async_clear_statistics.assert_called_once_with(["openfeed:acct_x_payments"])


def test_meter_key_salted_hides_nmi() -> None:
    from custom_components.openfeed.statistics import meter_key, meter_label

    meter = {"meterId": "M=", "nationalMeteringId": "QB12345678"}
    salted = meter_key(meter, "salt-a")
    assert salted.startswith("meter_") and "qb12345678" not in salted
    assert meter_key(meter, "salt-a") == salted
    assert meter_key(meter, "salt-b") != salted
    # No NMI shared: the meter ID is hashed with the salt instead.
    assert meter_key({"meterId": "M="}, "salt-a").startswith("meter_")
    # Legacy entries keep the NMI key their statistics already use.
    assert meter_key(meter) == "nmi_qb12345678"
    assert meter_label(meter) == "Meter ending 5678"
    assert meter_label({"meterId": "M="}) == "Meter"
