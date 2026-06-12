"""Aerodrome Slipstream USDC-CBBTC yield strategy on Base."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from almanak.framework.intents import Intent
from almanak.framework.market import (
    IndicatorUnavailableError,
    MarketSnapshot,
    MarketSnapshotError,
    PriceUnavailableError,
)
from almanak.framework.strategies import IntentStrategy, almanak_strategy

logger = logging.getLogger(__name__)


@almanak_strategy(
    name="aerodrome_slipstream_usdc_cbbtc_yield",
    description="Conservative concentrated-liquidity yield strategy for CBBTC/USDC on Aerodrome Slipstream",
    version="1.0.0",
    author="Almanak",
    tags=["lp", "aerodrome", "slipstream", "base", "yield", "risk-managed"],
    supported_chains=["base"],
    supported_protocols=["aerodrome_slipstream"],
    intent_types=["LP_OPEN", "LP_CLOSE", "SWAP", "HOLD"],
    default_chain="base",
    quote_asset="USD",
)
class AerodromeSlipstreamUsdcCbbtcYieldStrategy(IntentStrategy):
    """Risk-managed concentrated LP strategy for the CBBTC/USDC Slipstream pool."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        def cfg(key: str, default: Any) -> Any:
            if isinstance(self.config, dict):
                return self.config.get(key, default)
            return getattr(self.config, key, default)

        self.execution_chain = str(cfg("chain", "base"))
        self.protocol = str(cfg("protocol", "aerodrome_slipstream"))
        self.pool = str(cfg("pool", "USDC/CBBTC/100"))
        self.base_token = str(cfg("base_token", "CBBTC"))
        self.quote_token = str(cfg("quote_token", "USDC"))

        pool_parts = [part.strip() for part in self.pool.split("/") if part.strip()]
        self.pool_token0 = pool_parts[0] if len(pool_parts) > 0 else self.quote_token
        self.pool_token1 = pool_parts[1] if len(pool_parts) > 1 else self.base_token

        self.rsi_period = int(cfg("rsi_period", 14))
        self.rsi_timeframe = str(cfg("rsi_timeframe", "1h"))
        self.entry_rsi_min = Decimal(str(cfg("entry_rsi_min", "42")))
        self.entry_rsi_max = Decimal(str(cfg("entry_rsi_max", "58")))

        self.atr_period = int(cfg("atr_period", 14))
        self.atr_timeframe = str(cfg("atr_timeframe", "1h"))
        self.max_atr_entry_pct = Decimal(str(cfg("max_atr_entry_pct", "2.2")))
        self.max_atr_exit_pct = Decimal(str(cfg("max_atr_exit_pct", "3.5")))

        self.deploy_fraction = Decimal(str(cfg("deploy_fraction", "0.35")))
        self.max_deploy_usd = Decimal(str(cfg("max_deploy_usd", "10000")))
        self.min_position_usd = Decimal(str(cfg("min_position_usd", "1500")))
        self.reserve_quote_usd = Decimal(str(cfg("reserve_quote_usd", "500")))

        self.target_base_weight = Decimal(str(cfg("target_base_weight", "0.5")))
        self.rebalance_tolerance_pct = Decimal(str(cfg("rebalance_tolerance_pct", "0.12")))
        self.max_rebalance_swap_usd = Decimal(str(cfg("max_rebalance_swap_usd", "3000")))
        self.swap_max_slippage = Decimal(str(cfg("swap_max_slippage", "0.005")))

        self.base_range_width_pct = Decimal(str(cfg("base_range_width_pct", "8")))
        self.min_range_width_pct = Decimal(str(cfg("min_range_width_pct", "5")))
        self.max_range_width_pct = Decimal(str(cfg("max_range_width_pct", "15")))
        self.atr_width_multiplier = Decimal(str(cfg("atr_width_multiplier", "1.5")))

        self.projected_il_scenario_pct = Decimal(str(cfg("projected_il_scenario_pct", "15")))
        self.max_projected_il_pct = Decimal(str(cfg("max_projected_il_pct", "2.5")))

        self.max_position_age_hours = int(cfg("max_position_age_hours", 72))
        self.cooldown_minutes_after_close = int(cfg("cooldown_minutes_after_close", 45))

        self.force_action = str(cfg("force_action", "")).strip().lower()
        self.force_position_id = cfg("force_position_id", None)

        self._position_id: str | None = None
        self._range_lower: Decimal | None = None
        self._range_upper: Decimal | None = None
        self._pending_range: tuple[Decimal, Decimal] | None = None
        self._opened_at: datetime | None = None
        self._last_close_at: datetime | None = None

    def _now(self) -> datetime:
        return datetime.now(UTC)

    def _range_width_pct(self, atr_value_pct: Decimal) -> Decimal:
        dynamic = max(self.base_range_width_pct, atr_value_pct * self.atr_width_multiplier)
        return min(self.max_range_width_pct, max(self.min_range_width_pct, dynamic))

    def _safe_projected_il_pct(self, market: MarketSnapshot) -> Decimal | None:
        if not hasattr(market, "projected_il"):
            return None
        try:
            projected = market.projected_il(
                self.base_token,
                self.quote_token,
                self.projected_il_scenario_pct,
                weight_a=self.target_base_weight,
                weight_b=Decimal("1") - self.target_base_weight,
            )
            value = getattr(projected, "il_percent", None)
            if value is None:
                return None
            return Decimal(str(value))
        except (ValueError, AttributeError, MarketSnapshotError):
            return None

    def _entry_timing_ok(self, rsi_value: Decimal, atr_value_pct: Decimal) -> bool:
        return (
            self.entry_rsi_min <= rsi_value <= self.entry_rsi_max
            and atr_value_pct <= self.max_atr_entry_pct
        )

    def _rebalance_swap_intent(self, base_usd: Decimal, quote_usd: Decimal) -> Intent | None:
        total_usd = base_usd + quote_usd
        if total_usd <= 0:
            return None

        current_base_weight = base_usd / total_usd
        diff = current_base_weight - self.target_base_weight
        tolerance = self.rebalance_tolerance_pct
        if abs(diff) <= tolerance:
            return None

        target_base_usd = total_usd * self.target_base_weight
        if diff > 0:
            swap_usd = min(base_usd - target_base_usd, self.max_rebalance_swap_usd)
            if swap_usd <= 0:
                return None
            return Intent.swap(
                from_token=self.base_token,
                to_token=self.quote_token,
                amount_usd=swap_usd,
                max_slippage=self.swap_max_slippage,
                protocol=self.protocol,
                chain=self.execution_chain,
            )

        swap_usd = min(target_base_usd - base_usd, self.max_rebalance_swap_usd)
        if swap_usd <= 0:
            return None
        return Intent.swap(
            from_token=self.quote_token,
            to_token=self.base_token,
            amount_usd=swap_usd,
            max_slippage=self.swap_max_slippage,
            protocol=self.protocol,
            chain=self.execution_chain,
        )

    def _build_open_intent(
        self,
        pair_price: Decimal,
        base_price: Decimal,
        quote_price: Decimal,
        atr_value_pct: Decimal,
        base_balance_usd: Decimal,
        quote_balance_usd: Decimal,
    ) -> Intent | None:
        total_usd = base_balance_usd + quote_balance_usd
        deploy_budget = min(total_usd * self.deploy_fraction, self.max_deploy_usd)
        deploy_budget = min(deploy_budget, max(total_usd - self.reserve_quote_usd, Decimal("0")))
        if deploy_budget < self.min_position_usd:
            return None

        target_base_usd = deploy_budget * self.target_base_weight
        target_quote_usd = deploy_budget - target_base_usd
        if target_base_usd <= 0 or target_quote_usd <= 0:
            return None

        base_amount = target_base_usd / base_price
        quote_amount = target_quote_usd / quote_price
        if base_amount <= 0 or quote_amount <= 0:
            return None

        if self.pool_token0 == self.base_token and self.pool_token1 == self.quote_token:
            amount0 = base_amount
            amount1 = quote_amount
        elif self.pool_token0 == self.quote_token and self.pool_token1 == self.base_token:
            amount0 = quote_amount
            amount1 = base_amount
        else:
            return None

        width_pct = self._range_width_pct(atr_value_pct)
        width_fraction = width_pct / Decimal("100")
        range_lower = pair_price * (Decimal("1") - width_fraction)
        range_upper = pair_price * (Decimal("1") + width_fraction)
        if range_lower <= 0 or range_upper <= range_lower:
            return None

        self._pending_range = (range_lower, range_upper)
        return Intent.lp_open(
            pool=self.pool,
            amount0=amount0,
            amount1=amount1,
            range_lower=range_lower,
            range_upper=range_upper,
            protocol=self.protocol,
            chain=self.execution_chain,
        )

    def _forced_intent(self, market: MarketSnapshot) -> Intent:
        base_price = market.price(self.base_token)
        quote_price = market.price(self.quote_token)
        pair_price = base_price / quote_price

        if self.force_action == "open":
            base_balance = market.balance(self.base_token, price=base_price)
            quote_balance = market.balance(self.quote_token, price=quote_price)
            base_balance_usd = Decimal(str(base_balance.balance_usd))
            quote_balance_usd = Decimal(str(quote_balance.balance_usd))

            intent = self._build_open_intent(
                pair_price=pair_price,
                base_price=base_price,
                quote_price=quote_price,
                atr_value_pct=Decimal("1"),
                base_balance_usd=base_balance_usd,
                quote_balance_usd=quote_balance_usd,
            )
            if intent is not None:
                return intent

            if quote_balance_usd > 0:
                swap_usd = min(self.max_rebalance_swap_usd, Decimal("100"), quote_balance_usd)
                if swap_usd > 0:
                    return Intent.swap(
                        from_token=self.quote_token,
                        to_token=self.base_token,
                        amount_usd=swap_usd,
                        max_slippage=self.swap_max_slippage,
                        protocol=self.protocol,
                        chain=self.execution_chain,
                    )

            if base_balance_usd > 0:
                swap_usd = min(self.max_rebalance_swap_usd, Decimal("100"), base_balance_usd)
                if swap_usd > 0:
                    return Intent.swap(
                        from_token=self.base_token,
                        to_token=self.quote_token,
                        amount_usd=swap_usd,
                        max_slippage=self.swap_max_slippage,
                        protocol=self.protocol,
                        chain=self.execution_chain,
                    )

            return Intent.hold(reason="force_open skipped: insufficient pair-funded liquidity")

        if self.force_action == "rebalance_swap":
            base_balance = market.balance(self.base_token, price=base_price)
            quote_balance = market.balance(self.quote_token, price=quote_price)
            base_balance_usd = Decimal(str(base_balance.balance_usd))
            quote_balance_usd = Decimal(str(quote_balance.balance_usd))

            swap = self._rebalance_swap_intent(
                base_usd=base_balance_usd,
                quote_usd=quote_balance_usd,
            )
            if swap is not None:
                return swap

            if base_balance_usd <= 0 and quote_balance_usd <= 0:
                return Intent.hold(reason="force_rebalance_swap skipped: insufficient pair-funded liquidity")

            if quote_balance_usd > 0:
                return Intent.swap(
                    from_token=self.quote_token,
                    to_token=self.base_token,
                    amount_usd=min(self.max_rebalance_swap_usd, Decimal("100"), quote_balance_usd),
                    max_slippage=self.swap_max_slippage,
                    protocol=self.protocol,
                    chain=self.execution_chain,
                )

            return Intent.swap(
                from_token=self.base_token,
                to_token=self.quote_token,
                amount_usd=min(self.max_rebalance_swap_usd, Decimal("100"), base_balance_usd),
                max_slippage=self.swap_max_slippage,
                protocol=self.protocol,
                chain=self.execution_chain,
            )

        if self.force_action == "close":
            position_id = str(self.force_position_id or self._position_id or "0")
            return Intent.lp_close(
                position_id=position_id,
                pool=self.pool,
                collect_fees=True,
                protocol=self.protocol,
                chain=self.execution_chain,
            )

        raise ValueError(f"Unknown force_action: {self.force_action}")

    def decide(self, market: MarketSnapshot):
        if self.force_action:
            try:
                return self._forced_intent(market)
            except (
                ValueError,
                KeyError,
                ZeroDivisionError,
                PriceUnavailableError,
                IndicatorUnavailableError,
                MarketSnapshotError,
            ) as exc:
                return Intent.hold(reason=f"forced action skipped: {exc}")

        try:
            base_price = market.price(self.base_token)
            quote_price = market.price(self.quote_token)
            pair_price = base_price / quote_price
            rsi_data = market.rsi(self.base_token, period=self.rsi_period, timeframe=self.rsi_timeframe)
            atr_data = market.atr(self.base_token, period=self.atr_period, timeframe=self.atr_timeframe)
            rsi_value = Decimal(str(rsi_data.value))
            atr_value_pct = Decimal(str(atr_data.value_percent))
        except (
            ValueError,
            KeyError,
            ZeroDivisionError,
            PriceUnavailableError,
            IndicatorUnavailableError,
            MarketSnapshotError,
        ) as exc:
            return Intent.hold(reason=f"market data unavailable: {exc}")

        projected_il_pct = self._safe_projected_il_pct(market)

        if self._position_id is not None:
            if self._range_lower is not None and self._range_upper is not None:
                if pair_price < self._range_lower or pair_price > self._range_upper:
                    return Intent.lp_close(
                        position_id=self._position_id,
                        pool=self.pool,
                        collect_fees=True,
                        protocol=self.protocol,
                        chain=self.execution_chain,
                    )

            if atr_value_pct > self.max_atr_exit_pct:
                return Intent.lp_close(
                    position_id=self._position_id,
                    pool=self.pool,
                    collect_fees=True,
                    protocol=self.protocol,
                    chain=self.execution_chain,
                )

            if projected_il_pct is not None and projected_il_pct > self.max_projected_il_pct:
                return Intent.lp_close(
                    position_id=self._position_id,
                    pool=self.pool,
                    collect_fees=True,
                    protocol=self.protocol,
                    chain=self.execution_chain,
                )

            if self._opened_at is not None:
                max_age = timedelta(hours=self.max_position_age_hours)
                if self._now() - self._opened_at > max_age:
                    return Intent.lp_close(
                        position_id=self._position_id,
                        pool=self.pool,
                        collect_fees=True,
                        protocol=self.protocol,
                        chain=self.execution_chain,
                    )

            return Intent.hold(reason="position healthy")

        if self._last_close_at is not None:
            cooldown = timedelta(minutes=self.cooldown_minutes_after_close)
            if self._now() - self._last_close_at < cooldown:
                return Intent.hold(reason="cooldown after last close")

        if not self._entry_timing_ok(rsi_value, atr_value_pct):
            return Intent.hold(reason="entry timing gate not met")

        if projected_il_pct is not None and projected_il_pct > self.max_projected_il_pct:
            return Intent.hold(reason="projected impermanent loss above threshold")

        try:
            base_balance = market.balance(self.base_token, price=base_price)
            quote_balance = market.balance(self.quote_token, price=quote_price)
            base_balance_usd = Decimal(str(base_balance.balance_usd))
            quote_balance_usd = Decimal(str(quote_balance.balance_usd))
        except (ValueError, KeyError, MarketSnapshotError) as exc:
            return Intent.hold(reason=f"balance data unavailable: {exc}")

        if base_balance_usd + quote_balance_usd < self.min_position_usd:
            return Intent.hold(reason="insufficient capital")

        rebalance_intent = self._rebalance_swap_intent(base_balance_usd, quote_balance_usd)
        if rebalance_intent is not None:
            return rebalance_intent

        open_intent = self._build_open_intent(
            pair_price=pair_price,
            base_price=base_price,
            quote_price=quote_price,
            atr_value_pct=atr_value_pct,
            base_balance_usd=base_balance_usd,
            quote_balance_usd=quote_balance_usd,
        )
        if open_intent is None:
            return Intent.hold(reason="unable to size LP position")
        return open_intent

    def on_intent_executed(self, intent, success: bool, result):
        if not success:
            return
        intent_type = getattr(intent, "intent_type", None)
        if not intent_type:
            return

        if intent_type.value == "LP_OPEN":
            position_id = getattr(result, "position_id", None) if result else None
            if position_id is not None:
                self._position_id = str(position_id)
            if self._pending_range is not None:
                self._range_lower, self._range_upper = self._pending_range
            self._pending_range = None
            self._opened_at = self._now()

        if intent_type.value == "LP_CLOSE":
            self._position_id = None
            self._range_lower = None
            self._range_upper = None
            self._pending_range = None
            self._opened_at = None
            self._last_close_at = self._now()

    def get_persistent_state(self):
        return {
            "position_id": self._position_id,
            "range_lower": str(self._range_lower) if self._range_lower is not None else None,
            "range_upper": str(self._range_upper) if self._range_upper is not None else None,
            "opened_at": self._opened_at.isoformat() if self._opened_at is not None else None,
            "last_close_at": self._last_close_at.isoformat() if self._last_close_at is not None else None,
        }

    def load_persistent_state(self, state):
        if not state:
            return

        position_id = state.get("position_id")
        self._position_id = str(position_id) if position_id is not None else None

        range_lower = state.get("range_lower")
        range_upper = state.get("range_upper")
        self._range_lower = Decimal(str(range_lower)) if range_lower is not None else None
        self._range_upper = Decimal(str(range_upper)) if range_upper is not None else None

        opened_at = state.get("opened_at")
        if opened_at:
            try:
                self._opened_at = datetime.fromisoformat(opened_at)
            except ValueError:
                self._opened_at = None

        last_close_at = state.get("last_close_at")
        if last_close_at:
            try:
                self._last_close_at = datetime.fromisoformat(last_close_at)
            except ValueError:
                self._last_close_at = None

    def get_open_positions(self):
        from almanak.framework.teardown import PositionInfo, PositionType, TeardownPositionSummary

        positions = []
        if self._position_id is not None:
            positions.append(
                PositionInfo(
                    position_type=PositionType.LP,
                    position_id=self._position_id,
                    chain=self.execution_chain,
                    protocol=self.protocol,
                    value_usd=Decimal("0"),
                    details={"pool": self.pool},
                )
            )

        return TeardownPositionSummary(
            deployment_id=getattr(self, "deployment_id", "aerodrome_slipstream_usdc_cbbtc_yield"),
            timestamp=self._now(),
            positions=positions,
        )

    def generate_teardown_intents(self, mode=None, market=None):
        if self._position_id is None:
            return []
        return [
            Intent.lp_close(
                position_id=self._position_id,
                pool=self.pool,
                collect_fees=True,
                protocol=self.protocol,
                chain=self.execution_chain,
            )
        ]
