"""
Trading strategy runner
"""
import asyncio
import os
import time
from datetime import datetime, timedelta
from typing import Dict, List

import alpaca_trade_api as tradeapi
import asyncpg
import pygit2
from alpaca_trade_api.entity import Order
from alpaca_trade_api.polygon.entity import Ticker
from alpaca_trade_api.stream2 import StreamConn
from google.cloud import error_reporting
from pandas import DataFrame as df
from pytz import timezone
from pytz.tzinfo import DstTzInfo

import config
import trading_data
from market_data import (get_historical_data, get_tickers, prev_closes,
                         volume_today)
from models.new_trades import NewTrade
from strategies.base import Strategy
from strategies.momentun_short import MomentumShort
from tlog import tlog

error_logger = error_reporting.Client()


async def liquidate(
    symbol: str,
    symbol_position: int,
    trading_api: tradeapi,
    data_ws: StreamConn,
) -> None:

    if symbol_position:
        tlog(
            f"Trading over, trying to liquidating remaining position {symbol_position} in {symbol}"
        )
        try:
            trading_data.sell_indicators[symbol] = {"liquidation": 1}
            if symbol_position < 0:
                o = trading_api.submit_order(
                    symbol=symbol,
                    qty=str(symbol_position),
                    side="buy",
                    type="market",
                    time_in_force="day",
                )
            else:
                o = trading_api.submit_order(
                    symbol=symbol,
                    qty=str(symbol_position),
                    side="sell",
                    type="market",
                    time_in_force="day",
                )
            trading_data.open_orders[symbol] = o
        except Exception as e:
            error_logger.report_exception()
            tlog(f"failed to liquidate {symbol} w exception {e}")
    else:
        try:
            await data_ws.unsubscribe([f"A.{symbol}", f"AM.{symbol}"])
            # await trading_api.unsubscribe(trade_channels)
        except ValueError as e:
            tlog(f"failed to unsubscribe {symbol} w ValueError {e}")
            error_logger.report_exception()
        except Exception as e:
            tlog(f"failed to unsubscribe {symbol} w exception {e}")
            error_logger.report_exception()


async def should_cancel_order(order: Order, market_clock: datetime) -> bool:
    # Make sure the order's not too old
    submitted_at = order.submitted_at.astimezone(timezone("America/New_York"))
    order_lifetime = market_clock - submitted_at
    if market_clock > submitted_at and order_lifetime.seconds // 60 >= 1:
        return True

    return False


async def run(
    tickers: List[Ticker],
    strategies: List[Strategy],
    data_api: tradeapi,
    trading_api: tradeapi,
    data_ws: StreamConn,
    trading_ws: StreamConn,
) -> None:
    """main loop"""
    # Update initial state with information from tickers
    for ticker in tickers:
        symbol = ticker.ticker
        prev_closes[symbol] = ticker.prevDay["c"]
        volume_today[symbol] = ticker.day["v"]
    symbols = [ticker.ticker for ticker in tickers]
    tlog(f"Tracking {len(symbols)} symbols")

    minute_history: Dict[str, df] = get_historical_data(
        api=data_api, symbols=symbols,
    )

    # Cancel any existing open orders on watched symbols
    existing_orders = trading_api.list_orders(limit=500)
    for order in existing_orders:
        if order.symbol in symbols:
            tlog(f"cancel open order of {order.symbol}")
            trading_api.cancel_order(order.id)

    # Track any positions bought during previous executions
    existing_positions = trading_api.list_positions()
    for position in existing_positions:
        if position.symbol in symbols:
            trading_data.positions[position.symbol] = float(position.qty)
            # Recalculate cost basis and stop price
            trading_data.latest_cost_basis[position.symbol] = float(
                position.cost_basis
            )

    # Keep track of what we're buying/selling
    trade_channels = ["trade_updates"]
    data_channels = []
    for symbol in symbols:
        symbol_channels = ["A.{}".format(symbol), "AM.{}".format(symbol)]
        data_channels += symbol_channels
    tlog(f"Watching {len(symbols)} symbols.")

    # Use trade updates to keep track of our portfolio
    @trading_ws.on(r"trade_update")
    async def handle_trade_update(conn, channel, data):
        symbol = data.order["symbol"]

        last_order = trading_data.open_orders.get(symbol)
        if last_order is not None:
            tlog(f"trade update for {symbol}")
            event = data.event
            if event == "partial_fill":
                qty = int(data.order["filled_qty"])
                if data.order["side"] == "sell":
                    qty = qty * -1
                trading_data.positions[symbol] = trading_data.positions.get(
                    symbol, 0
                ) - trading_data.partial_fills.get(symbol, 0)
                trading_data.partial_fills[symbol] = qty
                trading_data.positions[symbol] += qty
                trading_data.open_orders[symbol] = Order(data.order)
            elif event == "fill":
                qty = int(data.order["filled_qty"])
                if data.order["side"] == "sell":
                    qty = qty * -1
                trading_data.positions[symbol] = trading_data.positions.get(
                    symbol, 0
                ) - trading_data.partial_fills.get(symbol, 0)
                trading_data.partial_fills[symbol] = 0
                trading_data.positions[symbol] += qty

                if data.order["side"] == "buy":
                    print(data.order)
                    db_trade = NewTrade(
                        algo_run_id=trading_data.open_order_strategy[
                            symbol
                        ].algo_run.run_id,
                        symbol=symbol,
                        qty=qty,
                        operation="buy",
                        price=float(data.order["filled_avg_price"]),
                        indicators=trading_data.buy_indicators[symbol],
                    )
                    await db_trade.save(
                        trading_data.db_conn_pool,
                        data.timestamp,
                        trading_data.stop_prices[symbol],
                        trading_data.target_prices[symbol],
                    )
                    trading_data.buy_indicators[symbol] = None
                if data.order["side"] == "sell":
                    if trading_data.sell_indicators[symbol] is not None:
                        db_trade = NewTrade(
                            algo_run_id=trading_data.open_order_strategy[
                                symbol
                            ].algo_run.run_id,
                            symbol=symbol,
                            qty=-qty,
                            operation="sell",
                            price=float(data.order["filled_avg_price"]),
                            indicators=trading_data.sell_indicators[symbol],
                        )
                        await db_trade.save(
                            trading_data.db_conn_pool,
                            data.timestamp,
                            trading_data.stop_prices[symbol],
                            trading_data.target_prices[symbol],
                        )
                    trading_data.sell_indicators[symbol] = None

                trading_data.open_orders[symbol] = None
                trading_data.open_order_strategy[symbol] = None
            elif event in ("canceled", "rejected"):
                trading_data.partial_fills[symbol] = 0
                trading_data.open_orders[symbol] = None
                trading_data.open_order_strategy[symbol] = None
        else:
            tlog(f"{data.event} trade update for {symbol} WITHOUT ORDER")

    @data_ws.on(r"A$")
    async def handle_second_bar(conn, channel, data):
        symbol = data.symbol

        # First, aggregate 1s bars for up-to-date MACD calculations
        original_ts = ts = data.start
        ts = ts.replace(
            second=0, microsecond=0
        )  # timedelta(seconds=ts.second, microseconds=ts.microsecond)

        try:
            current = minute_history[data.symbol].loc[ts]
        except KeyError:
            current = None

        if current is None:
            new_data = [
                data.open,
                data.high,
                data.low,
                data.close,
                data.volume,
            ]
        else:
            new_data = [
                current.open,
                data.high if data.high > current.high else current.high,
                data.low if data.low < current.low else current.low,
                data.close,
                current.volume + data.volume,
            ]
        minute_history[symbol].loc[ts] = new_data

        # Next, check for existing orders for the stock
        existing_order = trading_data.open_orders.get(symbol)
        if existing_order is not None:
            try:
                if await should_cancel_order(existing_order, original_ts):
                    # Cancel it so we can try again for a fill
                    tlog(
                        f"Cancel order id {existing_order.id} for {symbol} ts={original_ts} submission_ts={order.submitted_at}"
                    )
                    trading_api.cancel_order(existing_order.id)
                return
            except AttributeError:
                error_logger.report_exception()
                tlog(f"Attribute Error in symbol {symbol} w/ {existing_order}")

        # do we have a position?
        symbol_position = trading_data.positions.get(symbol, 0)

        # do we need to liquidate for the day?
        until_market_close = config.market_close - ts
        if until_market_close.seconds // 60 <= 15:
            tlog(
                f"{until_market_close.seconds // 60} minutes to market close for {symbol}"
            )
            await liquidate(symbol, symbol_position, trading_api, data_ws)

        # run strategies
        for s in strategies:
            if await s.run(
                symbol, symbol_position, minute_history[symbol], ts
            ):
                tlog(f"executed strategy {s.name} on {symbol}")
                return

    # Replace aggregated 1s bars with incoming 1m bars
    @data_ws.on(r"AM$")
    async def handle_minute_bar(conn, channel, data):
        ts = data.start
        ts -= timedelta(microseconds=ts.microsecond)
        minute_history[data.symbol].loc[ts] = [
            data.open,
            data.high,
            data.low,
            data.close,
            data.volume,
        ]
        volume_today[data.symbol] += data.volume

    try:
        await trading_ws.subscribe(trade_channels)
        await data_ws.subscribe(data_channels)
    except Exception as e:
        tlog(f"Exception {e}")
        error_logger.report_exception()


async def create_db_connection(dsn: str) -> None:
    trading_data.db_conn_pool = await asyncpg.create_pool(
        dsn=dsn, min_size=20, max_size=200
    )


async def start_strategies(
    trading_api: tradeapi,
    data_api: tradeapi,
    data_ws: StreamConn,
    trading_ws: StreamConn,
):
    tlog(f"setting up strategies")
    await create_db_connection(str(config.dsn))

    strategy_types = [MomentumShort]
    for strategy_type in strategy_types:
        tlog(f"initializing {strategy_type.name}")
        s = strategy_type(trading_api=trading_api, data_api=data_api)
        await s.create()

        trading_data.strategies.append(s)

    tickers = await get_tickers(data_api=data_api)
    await run(
        tickers=tickers,
        strategies=trading_data.strategies,
        trading_api=trading_api,
        data_api=data_api,
        data_ws=data_ws,
        trading_ws=trading_ws,
    )
    tlog("strategies ready to execute")


async def end_time(reason: str):
    for s in trading_data.strategies:
        await s.algo_run.update_end_time(
            pool=trading_data.db_conn_pool, end_reason=reason
        )


async def teardown_task(tz: DstTzInfo, ws: List[StreamConn]) -> None:
    dt = datetime.today().astimezone(tz)
    to_market_close = config.market_close - dt

    tlog(f"tear-down task waiting for market close: {to_market_close}")
    try:
        await asyncio.sleep(to_market_close.total_seconds() + 60 * 10)
    except asyncio.CancelledError:
        tlog(f"teardown_task() cancelled during sleep")
    else:
        tlog("tear down task starting")
        await end_time("market close")

        tlog("closing web-sockets")
        for w in ws:
            await w.close()

        asyncio.get_running_loop().stop()
    finally:
        tlog("tear down task done.")


def motd(filename: str, version: str) -> None:
    """Display welcome message"""

    print("+=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=+")
    tlog(f"{filename} {version} starting")
    print("+=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=+")
    tlog(f"TRADE_BUY_WINDOW: {config.trade_buy_window}")
    tlog(f"DSN: {config.dsn}")
    print("+=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=+")


def get_trading_windows(tz, api):
    """Get start and end time for trading"""

    today = datetime.today().astimezone(tz)
    today_str = datetime.today().astimezone(tz).strftime("%Y-%m-%d")

    calendar = api.get_calendar(start=today_str, end=today_str)[0]

    market_open = today.replace(
        hour=calendar.open.hour, minute=calendar.open.minute, second=0
    )
    market_close = today.replace(
        hour=calendar.close.hour, minute=calendar.close.minute, second=0
    )

    return market_open, market_close


def main():
    trading_data.build_label = pygit2.Repository("./").describe()
    trading_data.filename = os.path.basename(__file__)
    motd(filename=trading_data.filename, version=trading_data.build_label)

    _data_api = tradeapi.REST(
        base_url=config.prod_base_url,
        key_id=config.prod_api_key_id,
        secret_key=config.prod_api_secret,
    )
    _data_ws = tradeapi.StreamConn(
        base_url=config.prod_base_url,
        key_id=config.prod_api_key_id,
        secret_key=config.prod_api_secret,
    )
    nyc = timezone("America/New_York")
    config.market_open, config.market_close = get_trading_windows(
        nyc, _data_api
    )
    tlog(
        f"markets open {config.market_open} market close {config.market_close}"
    )

    # Wait until just before we might want to trade
    current_dt = datetime.today().astimezone(nyc)
    tlog(f"current time {current_dt}")

    if current_dt < config.market_close or config.bypass_market_schedule:
        if not config.bypass_market_schedule:
            to_market_open = config.market_open - current_dt
            tlog(f"waiting for market open: {to_market_open}")
            if to_market_open.total_seconds() > 0:
                time.sleep(to_market_open.total_seconds() + 1)

            tlog(
                f"market open, wait {config.market_cool_down_minutes} minutes"
            )
            since_market_open = (
                datetime.today().astimezone(nyc) - config.market_open
            )
            while (
                since_market_open.seconds // 60
                < config.market_cool_down_minutes
            ):
                time.sleep(1)
                since_market_open = (
                    datetime.today().astimezone(nyc) - config.market_open
                )

        tlog("ready to start")
        base_url = (
            config.prod_base_url
            if config.env == "PROD"
            else config.paper_base_url
        )
        api_key_id = (
            config.prod_api_key_id
            if config.env == "PROD"
            else config.paper_api_key_id
        )
        api_secret = (
            config.prod_api_secret
            if config.env == "PROD"
            else config.paper_api_secret
        )

        _trading_api = tradeapi.REST(
            base_url=base_url, key_id=api_key_id, secret_key=api_secret
        )
        _trade_ws = tradeapi.StreamConn(
            base_url=base_url, key_id=api_key_id, secret_key=api_secret,
        )
        asyncio.ensure_future(teardown_task(nyc, [_trade_ws, _data_ws]))

        asyncio.ensure_future(
            start_strategies(
                trading_api=_trading_api,
                data_api=_data_api,
                trading_ws=_trade_ws,
                data_ws=_data_ws,
            )
        )

        try:
            asyncio.get_event_loop().run_forever()
        except KeyboardInterrupt:
            tlog(f"Caught KeyboardInterrupt")
            asyncio.get_event_loop().run_until_complete(
                end_time("KeyboardInterrupt")
            )
        except Exception as e:
            tlog(f"Caught exception {str(e)}")
            asyncio.get_event_loop().run_until_complete(end_time(str(e)))

    else:
        tlog("missed market open time, try again next trading day, or bypass")


"""
starting
"""
main()
tlog("Done.")
