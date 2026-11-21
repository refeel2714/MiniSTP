"""A simulated paper account for demos and the sample database (never a real broker).

Behaves like the KIS paper account as measured in Phase 2: positions move on the
trade date, cash on the settlement date, commission truncated to 10 won.
"""
import datetime as dt

from ..calendar import BusinessCalendar, SettlementCycle
from ..fees import FeeSchedule
from ..instruments import Instrument
from .base import BrokerBalance, BrokerOrder, BrokerPosition, OrderAck


class SimBroker:
    def __init__(self, calendar: BusinessCalendar, fees: FeeSchedule, instruments: dict[str, Instrument],
                 cash: int, prices: dict[str, int], today: dt.date,
                 cycle: SettlementCycle = SettlementCycle.t_plus(2)):
        self.calendar, self.fees, self.instruments, self.cycle = calendar, fees, instruments, cycle
        self.cash = cash
        self.pending: list[tuple[dt.date, int]] = []  # (settle date, signed cash)
        self.positions: dict[str, list[int]] = {}      # symbol -> [qty, purchase amount]
        self.orders: dict[dt.date, dict[str, BrokerOrder]] = {}
        self.prices = dict(prices)
        self.today = today
        self.cash_skew = 0  # demo: a broker-side cash difference to reconcile

    def set_day(self, day: dt.date) -> None:
        self.today = day
        for sd, amt in [p for p in self.pending if p[0] <= day]:
            self.cash += amt
            self.pending.remove((sd, amt))

    def fill(self, order_no: str, qty: int, price: int) -> None:
        """Fill `qty` more shares of today's order at `price`."""
        o = self.orders[self.today][order_no]
        if o.filled_qty + qty > o.qty:
            raise ValueError("overfill")
        gross = qty * price
        costs = self.fees.costs(self.instruments[o.symbol], o.side == "SELL", gross)
        pos = self.positions.setdefault(o.symbol, [0, 0])
        if o.side == "BUY":
            pos[0] += qty
            pos[1] += gross
            signed = -(gross + costs.commission)
        else:
            cost = pos[1] * qty // pos[0]
            pos[0] -= qty
            pos[1] -= cost
            signed = gross - costs.commission - costs.taxes
        self.pending.append((self.calendar.settlement_date(self.today, self.cycle), signed))
        self.orders[self.today][order_no] = BrokerOrder(o.order_no, o.symbol, o.side, o.qty,
                                                        o.filled_qty + qty, o.filled_amount + gross, 0, 0)

    # --- gateway ---
    def get_balance(self) -> BrokerBalance:
        proj = self.cash + sum(a for _, a in self.pending)
        pos = {s: BrokerPosition(s, q, c) for s, (q, c) in self.positions.items() if q}
        return BrokerBalance(pos, self.cash + self.cash_skew, proj + self.cash_skew,
                             ({"simulated": True, "cash": self.cash},))

    def get_price(self, symbol: str) -> int:
        return self.prices[symbol]

    def place_order(self, symbol: str, side: str, qty: int, limit_price: int | None) -> OrderAck:
        day = self.orders.setdefault(self.today, {})
        no = f"{len(day) + 1:010d}"
        day[no] = BrokerOrder(no, symbol, side, qty, 0, 0, 0, 0)
        return OrderAck(no, "SIM")

    def cancel_order(self, ack: OrderAck) -> None:
        o = self.orders[self.today][ack.order_no]
        self.orders[self.today][ack.order_no] = BrokerOrder(o.order_no, o.symbol, o.side, o.qty, o.filled_qty,
                                                            o.filled_amount, o.qty - o.filled_qty, 0)

    def daily_orders(self, day: dt.date) -> list[BrokerOrder]:
        return list(self.orders.get(day, {}).values())
