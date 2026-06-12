import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from almanak.framework.data import PriceUnavailableError

from strategy import AerodromeSlipstreamUsdcCbbtcYieldStrategy


def _intent_value(intent) -> str:
    return getattr(getattr(intent, "intent_type", None), "value", "")


@pytest.fixture
def config():
    with open(Path(__file__).parent.parent / "config.json") as f:
        return json.load(f)


@pytest.fixture
def strategy(config):
    return AerodromeSlipstreamUsdcCbbtcYieldStrategy(
        config=config,
        chain=config.get("chain", "base"),
        wallet_address="0x" + "1" * 40,
    )


def _market(
    *,
    base_price: Decimal = Decimal("105000"),
    quote_price: Decimal = Decimal("1"),
    eth_price: Decimal = Decimal("2500"),
    rsi: Decimal = Decimal("50"),
    atr_pct: Decimal = Decimal("1.1"),
    base_balance: Decimal = Decimal("0.05"),
    quote_balance: Decimal = Decimal("6000"),
    eth_balance: Decimal = Decimal("2"),
    projected_il_pct: Decimal | None = Decimal("1.2"),
):
    market = MagicMock()

    def price_side_effect(token):
        if token == "CBBTC":
            return base_price
        if token == "USDC":
            return quote_price
        if token == "ETH":
            return eth_price
        raise ValueError(f"Unknown token {token}")

    def balance_side_effect(token, **kwargs):
        data = MagicMock()
        if token == "CBBTC":
            data.balance = base_balance
            data.balance_usd = base_balance * base_price
            return data
        if token == "USDC":
            data.balance = quote_balance
            data.balance_usd = quote_balance * quote_price
            return data
        if token == "ETH":
            data.balance = eth_balance
            data.balance_usd = eth_balance * eth_price
            return data
        raise ValueError(f"Unknown token {token}")

    market.price.side_effect = price_side_effect
    market.balance.side_effect = balance_side_effect

    rsi_data = MagicMock()
    rsi_data.value = rsi
    market.rsi.return_value = rsi_data

    atr_data = MagicMock()
    atr_data.value_percent = atr_pct
    market.atr.return_value = atr_data

    if projected_il_pct is not None:
        il = MagicMock()
        il.il_percent = projected_il_pct
        market.projected_il.return_value = il
    else:
        market.projected_il.side_effect = ValueError("il unavailable")

    return market


def test_market_data_unavailable_holds(strategy):
    market = _market()
    market.price.side_effect = PriceUnavailableError("base", reason="price unavailable")

    result = strategy.decide(market)

    assert _intent_value(result) == "HOLD"


def test_force_open_returns_lp_open(config):
    cfg = dict(config)
    cfg["force_action"] = "open"
    strat = AerodromeSlipstreamUsdcCbbtcYieldStrategy(config=cfg, chain="base", wallet_address="0x" + "1" * 40)

    result = strat.decide(_market())

    assert _intent_value(result) == "LP_OPEN"


def test_force_rebalance_swap_returns_swap(config):
    cfg = dict(config)
    cfg["force_action"] = "rebalance_swap"
    strat = AerodromeSlipstreamUsdcCbbtcYieldStrategy(config=cfg, chain="base", wallet_address="0x" + "1" * 40)

    result = strat.decide(_market(base_balance=Decimal("0.12"), quote_balance=Decimal("1000")))

    assert _intent_value(result) == "SWAP"


def test_force_open_holds_when_pair_balances_zero(config):
    cfg = dict(config)
    cfg["force_action"] = "open"
    strat = AerodromeSlipstreamUsdcCbbtcYieldStrategy(config=cfg, chain="base", wallet_address="0x" + "1" * 40)

    result = strat.decide(_market(base_balance=Decimal("0"), quote_balance=Decimal("0"), eth_balance=Decimal("1")))

    assert _intent_value(result) == "HOLD"
    assert "insufficient pair-funded liquidity" in result.reason


def test_force_rebalance_swap_holds_when_pair_balances_zero(config):
    cfg = dict(config)
    cfg["force_action"] = "rebalance_swap"
    strat = AerodromeSlipstreamUsdcCbbtcYieldStrategy(config=cfg, chain="base", wallet_address="0x" + "1" * 40)

    result = strat.decide(_market(base_balance=Decimal("0"), quote_balance=Decimal("0"), eth_balance=Decimal("1")))

    assert _intent_value(result) == "HOLD"
    assert "insufficient pair-funded liquidity" in result.reason


def test_force_close_returns_lp_close(config):
    cfg = dict(config)
    cfg["force_action"] = "close"
    cfg["force_position_id"] = "777"
    strat = AerodromeSlipstreamUsdcCbbtcYieldStrategy(config=cfg, chain="base", wallet_address="0x" + "1" * 40)

    result = strat.decide(_market())

    assert _intent_value(result) == "LP_CLOSE"
    assert result.position_id == "777"


def test_no_position_entry_timing_gate_holds(strategy):
    result = strategy.decide(_market(rsi=Decimal("70")))

    assert _intent_value(result) == "HOLD"
    assert "entry timing" in result.reason


def test_no_position_projected_il_gate_holds(strategy):
    result = strategy.decide(_market(projected_il_pct=Decimal("4")))

    assert _intent_value(result) == "HOLD"
    assert "impermanent loss" in result.reason


def test_no_position_rebalance_inventory_swaps(strategy):
    result = strategy.decide(_market(base_balance=Decimal("0.15"), quote_balance=Decimal("500")))

    assert _intent_value(result) == "SWAP"


def test_no_position_opens_lp_when_conditions_met(strategy):
    result = strategy.decide(_market())

    assert _intent_value(result) == "LP_OPEN"
    lower = int(result.range_lower)
    upper = int(result.range_upper)

    assert Decimal(str(result.range_lower)) == Decimal(lower)
    assert Decimal(str(result.range_upper)) == Decimal(upper)
    assert lower % strategy.pool_tick_spacing == 0
    assert upper % strategy.pool_tick_spacing == 0
    assert upper > lower


def test_open_position_range_breach_closes(strategy):
    strategy._position_id = "123"
    strategy._range_lower = Decimal("90000")
    strategy._range_upper = Decimal("100000")

    result = strategy.decide(_market(base_price=Decimal("105000")))

    assert _intent_value(result) == "LP_CLOSE"


def test_open_position_atr_risk_closes(strategy):
    strategy._position_id = "123"
    strategy._range_lower = Decimal("100000")
    strategy._range_upper = Decimal("110000")

    result = strategy.decide(_market(atr_pct=Decimal("4.1")))

    assert _intent_value(result) == "LP_CLOSE"


def test_open_position_age_limit_closes(strategy):
    strategy._position_id = "123"
    strategy._range_lower = Decimal("100000")
    strategy._range_upper = Decimal("110000")
    strategy._opened_at = datetime.now(UTC) - timedelta(hours=100)

    result = strategy.decide(_market())

    assert _intent_value(result) == "LP_CLOSE"


def test_open_position_healthy_holds(strategy):
    strategy._position_id = "123"
    strategy._range_lower = Decimal("100000")
    strategy._range_upper = Decimal("110000")
    strategy._opened_at = datetime.now(UTC) - timedelta(hours=1)

    result = strategy.decide(_market())

    assert _intent_value(result) == "HOLD"


def test_on_intent_executed_tracks_state(strategy):
    open_intent = MagicMock()
    open_intent.intent_type.value = "LP_OPEN"

    open_result = MagicMock()
    open_result.position_id = 999

    strategy._pending_range = (Decimal("95000"), Decimal("115000"))
    strategy.on_intent_executed(open_intent, True, open_result)

    assert strategy._position_id == "999"
    assert strategy._range_lower == Decimal("95000")
    assert strategy._range_upper == Decimal("115000")
    assert strategy._opened_at is not None

    close_intent = MagicMock()
    close_intent.intent_type.value = "LP_CLOSE"
    strategy.on_intent_executed(close_intent, True, MagicMock())

    assert strategy._position_id is None
    assert strategy._range_lower is None
    assert strategy._range_upper is None
    assert strategy._last_close_at is not None


def test_persistence_round_trip(strategy, config):
    strategy._position_id = "42"
    strategy._range_lower = Decimal("93000")
    strategy._range_upper = Decimal("113000")
    strategy._opened_at = datetime.now(UTC)
    strategy._last_close_at = datetime.now(UTC)

    state = strategy.get_persistent_state()

    fresh = AerodromeSlipstreamUsdcCbbtcYieldStrategy(
        config=config,
        chain="base",
        wallet_address="0x" + "1" * 40,
    )
    fresh.load_persistent_state(state)

    assert fresh._position_id == "42"
    assert fresh._range_lower == Decimal("93000")
    assert fresh._range_upper == Decimal("113000")
    assert fresh._opened_at is not None
    assert fresh._last_close_at is not None


def test_teardown_methods(strategy):
    strategy._position_id = "11"

    summary = strategy.get_open_positions()
    intents = strategy.generate_teardown_intents()

    assert len(summary.positions) == 1
    assert _intent_value(intents[0]) == "LP_CLOSE"

    strategy._position_id = None
    assert strategy.generate_teardown_intents() == []
