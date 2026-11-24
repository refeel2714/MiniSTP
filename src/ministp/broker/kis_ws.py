"""KIS real-time fill notices (실시간 체결통보, H0STCNI9 paper / H0STCNI0 live).

A notice carries one fill's own quantity and price, which the session books in
arrival order once the broker's cumulative order state (`daily_orders`) shows the
fill (R-76, v1.27). Duplicate notices are ignored, late ones wait for the query,
and fills without a notice are filled in from the query by a periodic poll.

Message format (official samples, koreainvestment/open-trading-api):
* control frames are JSON (subscribe answer with AES key/iv, PINGPONG);
* data frames are ``<encrypted 0|1>|<tr_id>|<count>|<payload>``; the payload is
  AES-256-CBC + base64 when encrypted, then ``^``-separated fields.
"""
import base64
import datetime as dt
import json
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

WS_URL = {"paper": "ws://ops.koreainvestment.com:31000", "live": "ws://ops.koreainvestment.com:21000"}
WS_PATH = "/tryitout"
TR_NOTICE = {"paper": "H0STCNI9", "live": "H0STCNI0"}
COLUMNS = ["CUST_ID", "ACNT_NO", "ODER_NO", "OODER_NO", "SELN_BYOV_CLS", "RCTF_CLS",
           "ODER_KIND", "ODER_COND", "STCK_SHRN_ISCD", "CNTG_QTY", "CNTG_UNPR",
           "STCK_CNTG_HOUR", "RFUS_YN", "CNTG_YN", "ACPT_YN", "BRNC_NO", "ODER_QTY",
           "ACNT_NAME", "ORD_COND_PRC", "ORD_EXG_GB", "POPUP_YN", "FILLER", "CRDT_CLS",
           "CRDT_LOAN_DATE", "CNTG_ISNM40", "ODER_PRC"]


def decrypt(key: str, iv: str, text: str) -> str:
    dec = Cipher(algorithms.AES(key.encode()), modes.CBC(iv.encode())).decryptor()
    raw = dec.update(base64.b64decode(text)) + dec.finalize()
    unpad = padding.PKCS7(128).unpadder()
    return (unpad.update(raw) + unpad.finalize()).decode("utf-8")


@dataclass(frozen=True)
class FillNotice:
    order_no: str
    symbol: str
    side: str          # BUY | SELL
    qty: int           # 체결수량 (fill) or 주문수량 (acceptance)
    price: int
    time: str          # HHMMSS from the exchange
    is_fill: bool      # CNTG_YN == "2"
    rejected: bool     # RFUS_YN == "Y"

    @classmethod
    def from_fields(cls, f: dict) -> "FillNotice":
        is_fill = f.get("CNTG_YN") == "2"
        qty = f.get("CNTG_QTY") if is_fill else f.get("ODER_QTY")
        price = f.get("CNTG_UNPR") if is_fill else f.get("ODER_PRC")
        return cls(order_no=f.get("ODER_NO", "").lstrip("0").rjust(10, "0") if f.get("ODER_NO") else "",
                   symbol=f.get("STCK_SHRN_ISCD", ""),
                   side="SELL" if f.get("SELN_BYOV_CLS") == "01" else "BUY",
                   qty=int(qty or 0), price=int(price or 0), time=f.get("STCK_CNTG_HOUR", ""),
                   is_fill=is_fill, rejected=f.get("RFUS_YN") == "Y")


def subscribe_message(approval_key: str, tr_id: str, hts_id: str, subscribe: bool = True) -> str:
    return json.dumps({"header": {"approval_key": approval_key, "custtype": "P",
                                  "tr_type": "1" if subscribe else "2", "content-type": "utf-8"},
                       "body": {"input": {"tr_id": tr_id, "tr_key": hts_id}}})


class NoticeDecoder:
    """Turns raw frames into ('pingpong', raw) | ('subscribed', ok, msg) | ('notices', [FillNotice])."""

    def __init__(self, tr_id: str):
        self.tr_id = tr_id
        self.key: str | None = None
        self.iv: str | None = None

    def handle(self, raw: str):
        if raw[:1] in ("0", "1"):
            parts = raw.split("|", 3)
            if len(parts) < 4 or parts[1] != self.tr_id:
                return ("ignored", raw[:40])
            payload = parts[3]
            if parts[0] == "1":
                if self.key is None:
                    raise ValueError("encrypted notice before the subscribe answer")
                payload = decrypt(self.key, self.iv, payload)
            values = payload.split("^")
            n = len(COLUMNS)
            rows = [dict(zip(COLUMNS, values[i:i + n])) for i in range(0, len(values) - n + 1, n)]
            return ("notices", [FillNotice.from_fields(r) for r in rows])
        msg = json.loads(raw)
        header = msg.get("header", {})
        if header.get("tr_id") == "PINGPONG":
            return ("pingpong", raw)
        body = msg.get("body") or {}
        out = body.get("output") or {}
        if out.get("key"):
            self.key, self.iv = out["key"], out["iv"]
        return ("subscribed", body.get("rt_cd") == "0", body.get("msg1", ""))


class NoticeFeed(threading.Thread):
    """Background thread: connect, subscribe, push events onto `events`, reconnect on loss.

    Events: ("feed", "up"|"down", detail, at) and ("notice", FillNotice, at).
    The thread never touches the ledger; the main loop does (single writer).
    """

    def __init__(self, env: str, hts_id: str, approval_key: Callable[[], str], events: queue.Queue,
                 connect=None, now=dt.datetime.now, sleep=time.sleep, max_backoff: float = 30.0):
        super().__init__(daemon=True, name="kis-notice-feed")
        if not hts_id:
            raise ValueError("HTS ID is required for fill notices")
        self.url = WS_URL[env] + WS_PATH
        self.tr_id = TR_NOTICE[env]
        self.hts_id = hts_id
        self.approval_key = approval_key
        self.events = events
        self._connect = connect
        self._now = now
        self._sleep = sleep
        self.max_backoff = max_backoff
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        connect = self._connect
        if connect is None:
            from websockets.sync.client import connect
        backoff = 1.0
        while not self._stop.is_set():
            try:
                with connect(self.url, open_timeout=10) as ws:
                    decoder = NoticeDecoder(self.tr_id)
                    ws.send(subscribe_message(self.approval_key(), self.tr_id, self.hts_id))
                    while not self._stop.is_set():
                        try:
                            raw = ws.recv(timeout=1)
                        except TimeoutError:
                            continue
                        kind, *rest = decoder.handle(raw if isinstance(raw, str) else raw.decode())
                        if kind == "pingpong":
                            ws.pong(rest[0].encode())
                        elif kind == "subscribed":
                            ok, msg = rest
                            self.events.put(("feed", "up" if ok else "down", msg, self._now()))
                            if not ok:
                                return
                            backoff = 1.0
                        elif kind == "notices":
                            for n in rest[0]:
                                self.events.put(("notice", n, self._now()))
            except Exception as exc:  # network loss, server close, bad frame
                if self._stop.is_set():
                    return
                self.events.put(("feed", "down", f"{type(exc).__name__}: {exc}", self._now()))
                self._sleep(backoff)
                backoff = min(backoff * 2, self.max_backoff)
