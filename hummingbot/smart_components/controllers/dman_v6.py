import time
import uuid
from decimal import Decimal
from typing import List, Optional, Set

import pandas_ta as ta  # noqa: F401

from hummingbot.core.data_type.common import OrderType, TradeType
from hummingbot.smart_components.executors.dca_executor.data_types import DCAConfig
from hummingbot.smart_components.executors.position_executor.data_types import TrailingStop
from hummingbot.smart_components.order_level_distributions.distributions import Distributions
from hummingbot.smart_components.strategy_frameworks.controller_base import ControllerConfigBase
from hummingbot.smart_components.strategy_frameworks.data_types import (
    BotAction,
    CreateDCAExecutorAction,
    StopDCAExecutorAction,
    StoreDCAExecutorAction,
)
from hummingbot.smart_components.strategy_frameworks.generic_strategy.generic_controller import GenericController


class DManV6Config(ControllerConfigBase):
    """
    Configuration required to run the PairsTrading strategy.
    """
    strategy_name: str = "dman_v6"
    exchange: str = "binance_perpetual"
    trading_pair: str = "DOGE-USDT"
    leverage: int = 20

    # DCA configuration
    dca_refresh_time: int = 60
    max_dca_per_side: int = 3
    min_distance_between_dca: float = 0.03
    order_amount: Decimal = Decimal("10")
    amount_ratio_increase: float = 1.5
    n_levels: int = 5

    top_order_start_spread: float = 0.001
    start_spread: float = 0.02
    spread_ratio_increase: float = 2.0
    time_limit: int = 60 * 60 * 24 * 7
    global_take_profit: Decimal = Decimal("0.02")
    global_stop_loss: Decimal = Decimal("0.1")
    global_trailing_stop: TrailingStop = TrailingStop(activation_price=Decimal("0.01"),
                                                      trailing_delta=Decimal("0.005"))
    activation_threshold: Optional[Decimal] = None


class DManV6(GenericController):
    def __init__(self, config: DManV6Config):
        super().__init__(config)
        self.config = config
        self.amounts = Distributions.geometric(n_levels=self.config.n_levels, start=float(self.config.order_amount),
                                               ratio=self.config.amount_ratio_increase)
        self.spreads = [Decimal(self.config.top_order_start_spread)] + Distributions.geometric(
            n_levels=self.config.n_levels - 1, start=self.config.start_spread,
            ratio=self.config.spread_ratio_increase)
        self.expired_dcas = set()

    def update_strategy_markets_dict(self, markets_dict: dict[str, Set] = {}):
        if self.config.exchange not in markets_dict:
            markets_dict[self.config.exchange] = {self.config.trading_pair}
        else:
            markets_dict[self.config.exchange].add(self.config.trading_pair)
        return markets_dict

    def determine_actions(self) -> [List[BotAction]]:
        """
        Determine actions based on the provided executor handler report.
        """
        if self.all_candles_ready:
            create_dca_proposal: List[BotAction] = self.create_actions_proposal()
            stop_dca_proposal: List[BotAction] = self.stop_actions_proposal()
            store_dca_proposal: List[StoreDCAExecutorAction] = self.store_actions_proposal()

            return create_dca_proposal + stop_dca_proposal + store_dca_proposal
        else:
            return []

    def stop_actions_proposal(self) -> List[BotAction]:
        """
        Create a list of actions to stop the DCA executors that have reached their time limit.
        """
        dca_executors_df = self._executor_handler_report.dca_executors
        proposal = []
        if dca_executors_df.empty:
            return proposal
        else:
            dcas_to_stop = dca_executors_df[
                (dca_executors_df["status"] == "ACTIVE") &
                (dca_executors_df["net_pnl_quote"] == 0) &
                (dca_executors_df["timestamp"] + self.config.dca_refresh_time < time.time()) &
                (dca_executors_df["filled_amount"] == 0)]
            for dca_id in dcas_to_stop["dca_id"].values:
                if dca_id in self.expired_dcas:
                    continue
                proposal.append(StopDCAExecutorAction(dca_id=dca_id))
                self.expired_dcas.add(dca_id)
            return proposal

    def create_actions_proposal(self) -> List[CreateDCAExecutorAction]:
        dca_executors_df = self._executor_handler_report.dca_executors
        close_price = self.get_close_price(self.config.trading_pair)

        create_long_dca_flag = False
        create_short_dca_flag = False

        if dca_executors_df.empty:
            create_long_dca_flag = True
            create_short_dca_flag = True
        else:
            active_dca_executors_df = dca_executors_df[dca_executors_df["status"] != "TERMINATED"]
            long_dcas = active_dca_executors_df[active_dca_executors_df["side"] == "BUY"]
            short_dcas = active_dca_executors_df[active_dca_executors_df["side"] == "SELL"]
            n_long_dcas = len(long_dcas)
            n_short_dcas = len(short_dcas)
            min_long_dca_average_price = long_dcas["current_position_average_price"].min()
            max_short_dca_average_price = short_dcas["current_position_average_price"].max()
            if n_long_dcas == 0:
                create_long_dca_flag = True
            elif n_long_dcas < self.config.max_dca_per_side and min_long_dca_average_price != Decimal("0") and float(close_price) < float(min_long_dca_average_price) * (1 - self.config.min_distance_between_dca):
                create_long_dca_flag = True
            if n_short_dcas == 0:
                create_short_dca_flag = True
            elif n_short_dcas < self.config.max_dca_per_side and max_short_dca_average_price != Decimal("0") and float(close_price) > float(max_short_dca_average_price) * (1 + self.config.min_distance_between_dca):
                create_short_dca_flag = True

        proposal = []
        if create_long_dca_flag:
            proposal.append(self.create_dca_action(TradeType.BUY, close_price))
        if create_short_dca_flag:
            proposal.append(self.create_dca_action(TradeType.SELL, close_price))

        return proposal

    def create_dca_action(self, trade_type, close_price):
        dca_id = str(uuid.uuid4())
        prices = [close_price * (1 - spread) if trade_type == TradeType.BUY else close_price * (1 + spread) for spread in self.spreads]
        amounts_usd = [amount / price for amount, price in zip(self.amounts, prices)]
        return CreateDCAExecutorAction(
            dca_config=DCAConfig(
                id=dca_id,
                timestamp=time.time(),
                exchange=self.config.exchange,
                trading_pair=self.config.trading_pair,
                side=trade_type,
                amounts_quote=amounts_usd,
                prices=prices,
                stop_loss=self.config.global_stop_loss,
                take_profit=self.config.global_take_profit,
                trailing_stop=self.config.global_trailing_stop,
                time_limit=self.config.time_limit,
                open_order_type=OrderType.LIMIT,
                leverage=self.config.leverage,
                activation_bounds=self.config.activation_threshold,
            ),
            dca_id=dca_id)

    def store_actions_proposal(self) -> List[StoreDCAExecutorAction]:
        """
        Create a list of actions to store the DCA executors that have been terminated.
        """
        dca_executors_df = self._executor_handler_report.dca_executors
        proposal = []
        if dca_executors_df.empty:
            return proposal
        else:
            dcas_to_store = dca_executors_df[dca_executors_df["status"] == "TERMINATED"]["dca_id"].values
            for dca_id in dcas_to_store:
                proposal.append(StoreDCAExecutorAction(dca_id=dca_id))
            return proposal
