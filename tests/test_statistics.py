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
