"""Read-only connection check against the KIS paper account. Run on your own PC.

    python scripts/kis_smoke.py

Keys can come from MINISTP_KIS_* environment variables; anything missing is asked
for interactively with hidden input and is never written to disk.

It issues (or reuses) a token and reads the balance. It never places orders and
prints no keys or account numbers, so the output is safe to share.
"""
import getpass
import os
import sys

from ministp.broker import KisClient, KisConfig, KisError

PROMPTS = [
    ("MINISTP_KIS_APP_KEY", "모의투자 앱키"),
    ("MINISTP_KIS_APP_SECRET", "모의투자 앱시크릿"),
    ("MINISTP_KIS_ACCOUNT", "모의투자 계좌번호 (예: 12345678-01)"),
]


def ask_missing(environ: dict) -> dict:
    """Ask for anything not set in the environment. Input is hidden and not saved."""
    env = dict(environ)
    for key, label in PROMPTS:
        if not env.get(key):
            env[key] = getpass.getpass(f"{label} (입력 내용은 화면에 표시되지 않아요): ").strip()
    return env


def main() -> int:
    try:
        cfg = KisConfig.from_env(ask_missing(os.environ))
    except KisError as e:
        print(f"설정 오류: {e}")
        return 2
    print(f"서버: {cfg.env} ({cfg.base_url})")
    client = KisClient(cfg)
    try:
        client.token()
        print("토큰: OK")
        bal = client.get_balance()
    except KisError as e:
        print(f"실패: {e}")
        return 1
    print(f"보유 종목 수: {len(bal.positions)}")
    for p in bal.positions.values():
        print(f"  {p.symbol}  수량 {p.qty:,}  매입금액 {p.purchase_amount:,}")
    print(f"예수금(D+0): {bal.cash_d0:,}")
    print(f"결제 후 예상 예수금(D+2, 필드 prvs_rcdl_excc_amt): {bal.cash_projected:,}")
    summary = bal.raw[-1]["output2"][0]
    print("output2 필드 (값 확인용, 실측 항목):")
    for k in ("dnca_tot_amt", "nxdy_excc_amt", "prvs_rcdl_excc_amt", "tot_evlu_amt", "pchs_amt_smtl_amt"):
        print(f"  {k} = {summary.get(k)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
