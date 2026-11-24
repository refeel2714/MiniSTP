"""한국투자증권 KIS Open API client (read-only for now).

Keys never live in this repository. They come from environment variables on the
owner's machine. Connecting to the live (실전) server needs two explicit switches.

Field names follow the official samples (koreainvestment/open-trading-api). The
cash fields of the balance response are to be confirmed against the first real
paper-account response (spec README, 남은 실측 항목).
"""
import datetime as dt
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .base import BrokerBalance, BrokerOrder, BrokerPosition, OrderAck, OrderOutcomeUnknown, OrderRejected

PAPER_URL = "https://openapivts.koreainvestment.com:29443"
LIVE_URL = "https://openapi.koreainvestment.com:9443"
BALANCE_PATH = "/uapi/domestic-stock/v1/trading/inquire-balance"
TR_BALANCE = {"paper": "VTTC8434R", "live": "TTTC8434R"}
ORDER_PATH = "/uapi/domestic-stock/v1/trading/order-cash"
TR_ORDER = {("paper", "BUY"): "VTTC0012U", ("paper", "SELL"): "VTTC0011U",
            ("live", "BUY"): "TTTC0012U", ("live", "SELL"): "TTTC0011U"}
CANCEL_PATH = "/uapi/domestic-stock/v1/trading/order-rvsecncl"
TR_CANCEL = {"paper": "VTTC0013U", "live": "TTTC0013U"}
DAILY_PATH = "/uapi/domestic-stock/v1/trading/inquire-daily-ccld"
TR_DAILY = {"paper": "VTTC0081R", "live": "TTTC0081R"}
PRICE_PATH = "/uapi/domestic-stock/v1/quotations/inquire-price"
TR_PRICE = "FHKST01010100"
MAX_PAGES = 20
# Client-side pacing. KIS answers EGW00201 ("초당 거래건수를 초과") when calls come
# too fast; paper accounts allow far fewer calls per second than live ones.
MIN_INTERVAL = {"paper": 0.6, "live": 0.06}
RATE_LIMIT_CODE = "EGW00201"
RATE_LIMIT_RETRIES = 3


class KisError(RuntimeError):
    pass


class KisOrderRejected(KisError, OrderRejected):
    """KIS answered an order or cancel with rt_cd != 0 (R-103)."""

    def __init__(self, message: str, code: str = ""):
        KisError.__init__(self, message)
        self.code = code


class LiveTradingLocked(RuntimeError):
    """Live server requested without both MINISTP_KIS_ENV=live and MINISTP_ALLOW_LIVE=1."""


DEFAULT_PREFIX = "MINISTP_KIS"   # environment variables of the default account
DEFAULT_TOKEN_CACHE = Path.home() / ".ministp" / "kis_token.json"


@dataclass(frozen=True)
class KisConfig:
    app_key: str = field(repr=False)
    app_secret: str = field(repr=False)
    account_no: str = field(repr=False)   # 8자리 종합계좌번호 (CANO)
    product_code: str = "01"              # 계좌상품코드 (ACNT_PRDT_CD)
    hts_id: str = field(default="", repr=False)
    env: str = "paper"                    # paper | live
    token_cache: Path = DEFAULT_TOKEN_CACHE
    trace: Path | None = None             # MINISTP_KIS_TRACE: 주문·취소 응답과 주문내역 변화를 한 줄씩 (모의 실측용)

    @classmethod
    def from_env(cls, environ=os.environ, prefix: str = DEFAULT_PREFIX) -> "KisConfig":
        """`prefix` names one account's variables (다계좌): `MINISTP_KIS` reads `MINISTP_KIS_APP_KEY`,
        `MINISTP_KIS_2` reads `MINISTP_KIS_2_APP_KEY`, and so on. The server (`MINISTP_KIS_ENV`) and
        the live lock are shared by every account."""
        missing = [f"{prefix}_{k}" for k in ("APP_KEY", "APP_SECRET", "ACCOUNT") if not environ.get(f"{prefix}_{k}")]
        if missing:
            raise KisError(f"missing environment variables: {', '.join(missing)}")
        account = environ[f"{prefix}_ACCOUNT"].replace("-", "")
        if len(account) not in (8, 10) or not account.isdigit():
            raise KisError(f"{prefix}_ACCOUNT must be 8 digits, or 10 digits / 8-2 with product code")
        env = trading_env(environ)
        if env == "live" and environ.get("MINISTP_ALLOW_LIVE") != "1":
            raise LiveTradingLocked("live server needs MINISTP_ALLOW_LIVE=1 as well (Phase 4 only)")
        extra = {} if prefix == DEFAULT_PREFIX else {   # a token belongs to one app key: one cache file each
            "token_cache": DEFAULT_TOKEN_CACHE.with_name(f"kis_token_{prefix.removeprefix('MINISTP_').lower()}.json")}
        return cls(app_key=environ[f"{prefix}_APP_KEY"], app_secret=environ[f"{prefix}_APP_SECRET"],
                   account_no=account[:8], product_code=account[8:] or "01",
                   hts_id=environ.get(f"{prefix}_HTS_ID", ""), env=env,
                   trace=Path(environ["MINISTP_KIS_TRACE"]) if environ.get("MINISTP_KIS_TRACE") else None, **extra)

    @property
    def base_url(self) -> str:
        return LIVE_URL if self.env == "live" else PAPER_URL


def trading_env(environ=os.environ) -> str:
    """Which server this shell is set up for: 'paper' (the default) or 'live'.

    The one place MINISTP_KIS_ENV is read, so the commands and the screens (실전 테두리) cannot disagree.
    Being set to 'live' is not permission to trade there: that takes MINISTP_ALLOW_LIVE=1 as well."""
    env = environ.get("MINISTP_KIS_ENV", "paper")
    if env not in ("paper", "live"):
        raise KisError("MINISTP_KIS_ENV must be 'paper' or 'live'")
    return env


# transport(method, url, headers, body_or_None) -> (status, response_headers, json_body)
Transport = Callable[[str, str, dict, dict | None], tuple[int, dict, dict]]


def urllib_transport(method: str, url: str, headers: dict, body: dict | None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"content-type": "application/json; charset=utf-8", **headers})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read() or b"{}")
        except ValueError:
            payload = {}
        return e.code, {k.lower(): v for k, v in e.headers.items()}, payload


def _int(v) -> int:
    return int(str(v or "0").split(".")[0])


class KisClient:
    def __init__(self, config: KisConfig, transport: Transport = urllib_transport,
                 now: Callable[[], dt.datetime] = dt.datetime.now,
                 sleep: Callable[[float], None] = time.sleep,
                 monotonic: Callable[[], float] = time.monotonic):
        self.cfg = config
        self._transport = transport
        self._now = now
        self._sleep = sleep
        self._monotonic = monotonic
        self._last_call: float | None = None
        self._token: tuple[str, dt.datetime] | None = None
        self._traced_orders: list | None = None

    def _trace(self, kind: str, **rec) -> None:
        """Appends one JSON line to the trace file, if one is set. Keys, tokens and the account number never go in."""
        if self.cfg.trace is None:
            return
        rec = {"at": self._now().isoformat(timespec="seconds"), "kind": kind, "env": self.cfg.env} | rec
        try:
            self.cfg.trace.parent.mkdir(parents=True, exist_ok=True)
            with self.cfg.trace.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        except OSError:
            pass                      # a trace must never stop an order

    # ----- auth -----

    def _load_cached_token(self) -> tuple[str, dt.datetime] | None:
        try:
            data = json.loads(self.cfg.token_cache.read_text())
        except (OSError, ValueError):
            return None
        if data.get("env") != self.cfg.env or data.get("app_key_tail") != self.cfg.app_key[-4:]:
            return None
        return data["token"], dt.datetime.fromisoformat(data["expires"])

    def _save_cached_token(self, token: str, expires: dt.datetime) -> None:
        path = self.cfg.token_cache
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"env": self.cfg.env, "app_key_tail": self.cfg.app_key[-4:],
                                    "token": token, "expires": expires.isoformat()}))
        try:
            path.chmod(0o600)
        except OSError:
            pass

    def token(self) -> str:
        """Reuse a cached token; KIS allows one new token per minute."""
        margin = dt.timedelta(minutes=10)
        for cand in (self._token, self._load_cached_token()):
            if cand and cand[1] - margin > self._now():
                self._token = cand
                return cand[0]
        status, _, body = self._call("POST", f"{self.cfg.base_url}/oauth2/tokenP", {}, {
            "grant_type": "client_credentials", "appkey": self.cfg.app_key,
            "appsecret": self.cfg.app_secret})
        if status != 200 or "access_token" not in body:
            raise KisError(f"token request failed ({status}): {body.get('error_description') or body.get('msg1') or ''}")
        expires = dt.datetime.strptime(body["access_token_token_expired"], "%Y-%m-%d %H:%M:%S")
        self._token = (body["access_token"], expires)
        self._save_cached_token(*self._token)
        return body["access_token"]

    def approval_key(self) -> str:
        """WebSocket 접속키 (실시간 체결통보용). Valid for a day; requested per connection."""
        status, _, body = self._call("POST", f"{self.cfg.base_url}/oauth2/Approval", {}, {
            "grant_type": "client_credentials", "appkey": self.cfg.app_key,
            "secretkey": self.cfg.app_secret})
        if status != 200 or "approval_key" not in body:
            raise KisError(f"approval key request failed ({status}): {body.get('msg1') or ''}")
        return body["approval_key"]

    def _call(self, method: str, url: str, headers: dict, body: dict | None,
              retry_rate_limit: bool = True):
        """Pace every request; retry rate-limit rejections for safe (non-order) calls."""
        for attempt in range(RATE_LIMIT_RETRIES + 1):
            if self._last_call is not None:
                wait = MIN_INTERVAL[self.cfg.env] - (self._monotonic() - self._last_call)
                if wait > 0:
                    self._sleep(wait)
            result = self._transport(method, url, headers, body)
            self._last_call = self._monotonic()
            limited = result[2].get("msg_cd") == RATE_LIMIT_CODE
            if not (limited and retry_rate_limit and attempt < RATE_LIMIT_RETRIES):
                return result
            self._sleep(1.0 * (attempt + 1))
        return result

    def _headers(self, tr_id: str, tr_cont: str = "") -> dict:
        return {"authorization": f"Bearer {self.token()}", "appkey": self.cfg.app_key,
                "appsecret": self.cfg.app_secret, "tr_id": tr_id, "custtype": "P",
                "tr_cont": tr_cont}

    @staticmethod
    def _check(tr_id: str, status: int, body: dict) -> None:
        if status != 200 or body.get("rt_cd") != "0":
            raise KisError(f"{tr_id} failed ({status}) {body.get('msg_cd', '')}: {body.get('msg1', '')}")

    def _get(self, path: str, tr_id: str, params: dict, tr_cont: str = "") -> tuple[dict, dict]:
        url = f"{self.cfg.base_url}{path}?{urllib.parse.urlencode(params)}"
        status, resp_headers, body = self._call("GET", url, self._headers(tr_id, tr_cont), None)
        self._check(tr_id, status, body)
        return resp_headers, body

    def _post(self, path: str, tr_id: str, body: dict) -> dict:
        """Orders and cancels. Never retried automatically: a retried order could double up.

        R-103 sorts the outcome. The broker answered no (a body with rt_cd other than 0, whatever the HTTP
        status, rate limit included) -> KisOrderRejected: nothing was placed. No usable answer (network error
        or timeout, a body without rt_cd) -> OrderOutcomeUnknown: the order may be at the broker, so MiniSTP
        looks for it in the broker's order list instead of sending it again."""
        try:
            headers = self._headers(tr_id)   # a token failure happens here, before anything is sent
        except (KisError, OSError, ValueError) as exc:
            raise KisOrderRejected(f"{tr_id}: 접속 토큰을 받지 못해 보내지 않았어요 ({exc})") from exc
        sent = {k: v for k, v in body.items() if k not in ("CANO", "ACNT_PRDT_CD")}
        try:
            status, _, resp = self._call("POST", f"{self.cfg.base_url}{path}", headers, body, retry_rate_limit=False)
        except (OSError, ValueError) as exc:          # URLError, timeouts and broken JSON are all OSError/ValueError
            self._trace("post", tr_id=tr_id, request=sent, error=f"{type(exc).__name__}: {exc}")
            raise OrderOutcomeUnknown(f"{tr_id}: 응답 없음 ({type(exc).__name__}: {exc})") from exc
        self._trace("post", tr_id=tr_id, request=sent, status=status, response=resp)
        code = str(resp.get("rt_cd", "")) if isinstance(resp, dict) else ""
        if code == "0" and status == 200:
            return resp
        if code and code != "0":                    # KIS answers errors with a body, often with HTTP 500
            raise KisOrderRejected(f"{tr_id} failed ({status}) {resp.get('msg_cd', '')}: {resp.get('msg1', '')}",
                                   resp.get("msg_cd", ""))
        raise OrderOutcomeUnknown(f"{tr_id}: 알 수 없는 응답 ({status}) {str(resp)[:120]}")

    def _paged(self, path: str, tr_id: str, params: dict) -> list[dict]:
        """Follow tr_cont continuation; returns every page body."""
        pages, tr_cont = [], ""
        for _ in range(MAX_PAGES):
            headers, body = self._get(path, tr_id, params, tr_cont)
            pages.append(body)
            if headers.get("tr_cont") not in ("M", "F"):
                return pages
            params = {**params, "CTX_AREA_FK100": body.get("ctx_area_fk100", ""),
                      "CTX_AREA_NK100": body.get("ctx_area_nk100", "")}
            tr_cont = "N"
            self._sleep(0.5)  # paper accounts have a low call limit
        raise KisError(f"{tr_id} continuation exceeded {MAX_PAGES} pages")

    # ----- queries -----

    def get_balance(self) -> BrokerBalance:
        """주식잔고조회 with continuation (paper: 20 rows per page)."""
        params = {"CANO": self.cfg.account_no, "ACNT_PRDT_CD": self.cfg.product_code,
                  "AFHR_FLPR_YN": "N", "OFL_YN": "", "INQR_DVSN": "02", "UNPR_DVSN": "01",
                  "FUND_STTL_ICLD_YN": "N", "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "00",
                  "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""}
        raw = self._paged(BALANCE_PATH, TR_BALANCE[self.cfg.env], params)
        rows = [r for body in raw for r in (body.get("output1") or [])]
        summaries = [body["output2"][0] for body in raw if body.get("output2")]
        summary = summaries[-1] if summaries else None
        if summary is None:
            raise KisError("balance response had no output2 summary")

        positions = {}
        for r in rows:
            qty = _int(r.get("hldg_qty"))
            if qty:
                positions[r["pdno"]] = BrokerPosition(r["pdno"], qty, _int(r.get("pchs_amt")))
        return BrokerBalance(positions=positions,
                             cash_d0=_int(summary.get("dnca_tot_amt")),
                             cash_projected=_int(summary.get("prvs_rcdl_excc_amt")),
                             raw=tuple(raw))

    # ----- orders (spec R-40~43) -----

    def get_price(self, symbol: str) -> int:
        """현재가 (reference price for pre-trade risk on market orders)."""
        _, body = self._get(PRICE_PATH, TR_PRICE,
                            {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol})
        return _int(body["output"]["stck_prpr"])

    def place_order(self, symbol: str, side: str, qty: int, limit_price: int | None) -> OrderAck:
        """현금 주문. limit_price=None means a market order (시장가)."""
        if qty <= 0:
            raise KisError("qty must be positive")
        body = {"CANO": self.cfg.account_no, "ACNT_PRDT_CD": self.cfg.product_code,
                "PDNO": symbol, "ORD_DVSN": "01" if limit_price is None else "00",
                "ORD_QTY": str(qty), "ORD_UNPR": "0" if limit_price is None else str(limit_price),
                "EXCG_ID_DVSN_CD": "KRX", "SLL_TYPE": "01" if side == "SELL" else "",
                "CNDT_PRIC": ""}
        resp = self._post(ORDER_PATH, TR_ORDER[(self.cfg.env, side)], body)
        try:
            out = resp["output"]
            return OrderAck(order_no=out["ODNO"], org_no=out["KRX_FWDG_ORD_ORGNO"])
        except (KeyError, TypeError) as exc:   # rt_cd 0 but no order number: placed, number unknown
            raise OrderOutcomeUnknown(f"주문은 받았다는데 주문번호가 없는 응답: {exc}") from exc

    def cancel_order(self, ack: OrderAck) -> None:
        """잔량 전부 취소."""
        body = {"CANO": self.cfg.account_no, "ACNT_PRDT_CD": self.cfg.product_code,
                "KRX_FWDG_ORD_ORGNO": ack.org_no, "ORGN_ODNO": ack.order_no, "ORD_DVSN": "00",
                "RVSE_CNCL_DVSN_CD": "02", "ORD_QTY": "0", "ORD_UNPR": "0",
                "QTY_ALL_ORD_YN": "Y", "EXCG_ID_DVSN_CD": "KRX"}
        self._post(CANCEL_PATH, TR_CANCEL[self.cfg.env], body)

    def daily_orders(self, day: dt.date) -> list[BrokerOrder]:
        """주식일별주문체결조회 for one day: cumulative fill state per order."""
        ymd = day.strftime("%Y%m%d")
        params = {"CANO": self.cfg.account_no, "ACNT_PRDT_CD": self.cfg.product_code,
                  "INQR_STRT_DT": ymd, "INQR_END_DT": ymd, "SLL_BUY_DVSN_CD": "00", "PDNO": "",
                  "CCLD_DVSN": "00", "INQR_DVSN": "01", "INQR_DVSN_3": "00", "ORD_GNO_BRNO": "",
                  "ODNO": "", "INQR_DVSN_1": "", "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""}
        out = []
        for body in self._paged(DAILY_PATH, TR_DAILY[self.cfg.env], params):
            for r in body.get("output1") or []:
                if not r.get("odno"):
                    continue
                out.append(BrokerOrder(
                    order_no=r["odno"], symbol=r["pdno"],
                    side="SELL" if r.get("sll_buy_dvsn_cd") == "01" else "BUY",
                    qty=_int(r.get("ord_qty")), filled_qty=_int(r.get("tot_ccld_qty")),
                    filled_amount=_int(r.get("tot_ccld_amt")),
                    canceled_qty=_int(r.get("cnc_cfrm_qty")), rejected_qty=_int(r.get("rjct_qty")),
                    org_no=r.get("ord_gno_brno") or ""))
        if self.cfg.trace is not None and out != self._traced_orders:   # only when the list changed
            self._traced_orders = list(out)
            self._trace("daily_orders", day=ymd, orders=[o.__dict__ for o in out])
        return out
