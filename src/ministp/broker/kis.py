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

from .base import BrokerBalance, BrokerPosition

PAPER_URL = "https://openapivts.koreainvestment.com:29443"
LIVE_URL = "https://openapi.koreainvestment.com:9443"
BALANCE_PATH = "/uapi/domestic-stock/v1/trading/inquire-balance"
TR_BALANCE = {"paper": "VTTC8434R", "live": "TTTC8434R"}
MAX_PAGES = 20


class KisError(RuntimeError):
    pass


class LiveTradingLocked(RuntimeError):
    """Live server requested without both MINISTP_KIS_ENV=live and MINISTP_ALLOW_LIVE=1."""


@dataclass(frozen=True)
class KisConfig:
    app_key: str = field(repr=False)
    app_secret: str = field(repr=False)
    account_no: str = field(repr=False)   # 8자리 종합계좌번호 (CANO)
    product_code: str = "01"              # 계좌상품코드 (ACNT_PRDT_CD)
    hts_id: str = field(default="", repr=False)
    env: str = "paper"                    # paper | live
    token_cache: Path = Path.home() / ".ministp" / "kis_token.json"

    @classmethod
    def from_env(cls, environ=os.environ) -> "KisConfig":
        missing = [k for k in ("MINISTP_KIS_APP_KEY", "MINISTP_KIS_APP_SECRET", "MINISTP_KIS_ACCOUNT")
                   if not environ.get(k)]
        if missing:
            raise KisError(f"missing environment variables: {', '.join(missing)}")
        account = environ["MINISTP_KIS_ACCOUNT"].replace("-", "")
        if len(account) not in (8, 10) or not account.isdigit():
            raise KisError("MINISTP_KIS_ACCOUNT must be 8 digits, or 10 digits / 8-2 with product code")
        env = environ.get("MINISTP_KIS_ENV", "paper")
        if env not in ("paper", "live"):
            raise KisError("MINISTP_KIS_ENV must be 'paper' or 'live'")
        if env == "live" and environ.get("MINISTP_ALLOW_LIVE") != "1":
            raise LiveTradingLocked("live server needs MINISTP_ALLOW_LIVE=1 as well (Phase 4 only)")
        return cls(app_key=environ["MINISTP_KIS_APP_KEY"], app_secret=environ["MINISTP_KIS_APP_SECRET"],
                   account_no=account[:8], product_code=account[8:] or "01",
                   hts_id=environ.get("MINISTP_KIS_HTS_ID", ""), env=env)

    @property
    def base_url(self) -> str:
        return LIVE_URL if self.env == "live" else PAPER_URL


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
                 sleep: Callable[[float], None] = time.sleep):
        self.cfg = config
        self._transport = transport
        self._now = now
        self._sleep = sleep
        self._token: tuple[str, dt.datetime] | None = None

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
        status, _, body = self._transport("POST", f"{self.cfg.base_url}/oauth2/tokenP", {}, {
            "grant_type": "client_credentials", "appkey": self.cfg.app_key,
            "appsecret": self.cfg.app_secret})
        if status != 200 or "access_token" not in body:
            raise KisError(f"token request failed ({status}): {body.get('error_description') or body.get('msg1') or ''}")
        expires = dt.datetime.strptime(body["access_token_token_expired"], "%Y-%m-%d %H:%M:%S")
        self._token = (body["access_token"], expires)
        self._save_cached_token(*self._token)
        return body["access_token"]

    def _get(self, path: str, tr_id: str, params: dict, tr_cont: str = "") -> tuple[dict, dict]:
        headers = {"authorization": f"Bearer {self.token()}", "appkey": self.cfg.app_key,
                   "appsecret": self.cfg.app_secret, "tr_id": tr_id, "custtype": "P",
                   "tr_cont": tr_cont}
        url = f"{self.cfg.base_url}{path}?{urllib.parse.urlencode(params)}"
        status, resp_headers, body = self._transport("GET", url, headers, None)
        if status != 200 or body.get("rt_cd") != "0":
            raise KisError(f"{tr_id} failed ({status}) {body.get('msg_cd', '')}: {body.get('msg1', '')}")
        return resp_headers, body

    # ----- queries -----

    def get_balance(self) -> BrokerBalance:
        """주식잔고조회 with continuation (paper: 20 rows per page)."""
        params = {"CANO": self.cfg.account_no, "ACNT_PRDT_CD": self.cfg.product_code,
                  "AFHR_FLPR_YN": "N", "OFL_YN": "", "INQR_DVSN": "02", "UNPR_DVSN": "01",
                  "FUND_STTL_ICLD_YN": "N", "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "00",
                  "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""}
        rows, summary, raw, tr_cont = [], None, [], ""
        for _ in range(MAX_PAGES):
            headers, body = self._get(BALANCE_PATH, TR_BALANCE[self.cfg.env], params, tr_cont)
            raw.append(body)
            rows.extend(body.get("output1") or [])
            if body.get("output2"):
                summary = body["output2"][0]
            if headers.get("tr_cont") not in ("M", "F"):
                break
            params = {**params, "CTX_AREA_FK100": body.get("ctx_area_fk100", ""),
                      "CTX_AREA_NK100": body.get("ctx_area_nk100", "")}
            tr_cont = "N"
            self._sleep(0.5)  # paper accounts have a low call limit
        else:
            raise KisError(f"balance continuation exceeded {MAX_PAGES} pages")
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
