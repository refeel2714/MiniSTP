"""KIS fill-notice frames: subscribe answer, AES payloads, ping, reconnect."""
import base64
import json
import queue

import pytest
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from ministp.broker.kis_ws import (COLUMNS, NoticeDecoder, NoticeFeed, decrypt,
                                   subscribe_message)

KEY, IV = "k" * 32, "i" * 16


def encrypt(text: str) -> str:
    pad = padding.PKCS7(128).padder()
    data = pad.update(text.encode()) + pad.finalize()
    enc = Cipher(algorithms.AES(KEY.encode()), modes.CBC(IV.encode())).encryptor()
    return base64.b64encode(enc.update(data) + enc.finalize()).decode()


def row(**kw) -> list[str]:
    base = dict.fromkeys(COLUMNS, "")
    base.update(CUST_ID="htsid", ACNT_NO="5012345601", ODER_NO="0000012345", SELN_BYOV_CLS="02",
                STCK_SHRN_ISCD="069500", CNTG_QTY="2", CNTG_UNPR="80000", STCK_CNTG_HOUR="093015",
                RFUS_YN="N", CNTG_YN="2", ACPT_YN="2", ODER_QTY="2", ODER_PRC="0")
    base.update(kw)
    return [base[c] for c in COLUMNS]


SUBSCRIBED = json.dumps({"header": {"tr_id": "H0STCNI9", "tr_key": "htsid", "encrypt": "Y"},
                         "body": {"rt_cd": "0", "msg_cd": "OPSP0000", "msg1": "SUBSCRIBE SUCCESS",
                                  "output": {"iv": IV, "key": KEY}}})
PING = json.dumps({"header": {"tr_id": "PINGPONG", "datetime": "20261123093000"}})


def frame(*rows) -> str:
    return f"1|H0STCNI9|{len(rows):03d}|" + encrypt("^".join(v for r in rows for v in r))


def test_decrypt_round_trip():
    assert decrypt(KEY, IV, encrypt("가나다^123")) == "가나다^123"


def test_decoder_flow():
    dec = NoticeDecoder("H0STCNI9")
    assert dec.handle(SUBSCRIBED) == ("subscribed", True, "SUBSCRIBE SUCCESS")
    kind, notices = dec.handle(frame(row(), row(ODER_NO="0000012346", SELN_BYOV_CLS="01",
                                            CNTG_YN="1", ODER_QTY="5", ODER_PRC="81000")))
    assert kind == "notices"
    fill, accept = notices
    assert (fill.order_no, fill.side, fill.qty, fill.price, fill.is_fill) == ("0000012345", "BUY", 2, 80_000, True)
    assert (accept.side, accept.qty, accept.price, accept.is_fill) == ("SELL", 5, 81_000, False)
    assert dec.handle(PING) == ("pingpong", PING)
    assert dec.handle("0|H0STCNT0|001|x^y")[0] == "ignored"


def test_encrypted_frame_before_key_is_an_error():
    with pytest.raises(ValueError):
        NoticeDecoder("H0STCNI9").handle(frame(row()))


def test_subscribe_message_uses_hts_id():
    m = json.loads(subscribe_message("AK", "H0STCNI9", "myhts"))
    assert m["header"]["approval_key"] == "AK" and m["header"]["tr_type"] == "1"
    assert m["body"]["input"] == {"tr_id": "H0STCNI9", "tr_key": "myhts"}


class FakeWs:
    def __init__(self, frames, feed):
        self.frames, self.feed, self.sent, self.pongs = list(frames), feed, [], []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def send(self, m):
        self.sent.append(m)

    def pong(self, data):
        self.pongs.append(data)

    def recv(self, timeout=None):
        if not self.frames:
            self.feed.stop()
            raise TimeoutError
        f = self.frames.pop(0)
        if isinstance(f, Exception):
            raise f
        return f


def test_feed_reconnects_and_pushes_events():
    events = queue.Queue()
    conns = []
    scripts = [[SUBSCRIBED, frame(row()), ConnectionResetError("lost")],
               [SUBSCRIBED, PING]]

    def connect(url, open_timeout):
        assert url == "ws://ops.koreainvestment.com:31000/tryitout"
        ws = FakeWs(scripts[len(conns)], feed)
        conns.append(ws)
        return ws

    feed = NoticeFeed("paper", "myhts", lambda: "AK", events, connect=connect, sleep=lambda s: None)
    feed.run()
    kinds = []
    while not events.empty():
        e = events.get()
        kinds.append((e[0], e[1] if e[0] == "feed" else e[1].order_no))
    assert kinds == [("feed", "up"), ("notice", "0000012345"), ("feed", "down"), ("feed", "up")]
    assert len(conns) == 2 and conns[1].pongs == [PING.encode()]
    assert json.loads(conns[0].sent[0])["body"]["input"]["tr_key"] == "myhts"
