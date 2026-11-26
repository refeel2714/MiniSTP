"""공통코드 (ERD v2.1): code values and their Korean names in one place.

The schema and the program are full of short codes (`SUSPENDED`, `BREAK`,
`FEE_DIFF`, `VALUATION_REVERSAL` ...). Their names used to live in people's heads
and in scattered comments. `code_groups` and `common_codes` hold them, and
`check()` proves that the table has not drifted from what the database and the
program really use:

* a column with a CHECK list allows exactly the codes of its group;
* a code the program defines (an enum, the event classes) has a row;
* every value actually stored in a group's column has a name.

Codes that the ledger itself references stay in their own tables: 계정과목 is
`accounts_chart`, tied to every journal line by a foreign key. A common code is
a label, and nothing breaks when its name changes. That is the line between the two.

A code is never deleted (old rows still carry it): it is renamed or switched off.
Groups marked `fixed` belong to the program, so only their names can be changed.
"""
import re
from dataclasses import dataclass

import psycopg
from psycopg import sql

from . import audit
from .master import GROUPS as MASTER_GROUPS
from .ops import OpsError

GROUP_ID_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,39}$")


@dataclass(frozen=True)
class CodeGroup:
    group_id: str
    name: str
    description: str
    used_by: str | None                      # table.column that stores these codes, if one does
    codes: tuple[tuple[str, str, str], ...]  # (code, name, description)
    fixed: bool = True                       # the program defines the codes; only names change


# The codes a new database gets (`init_schema`). An existing one gets the same rows from the migrations:
# 0002 inserted the codes of ERD v2.1, 0003 the three of the day close (v2.2), 0004 the three audit groups (v2.3),
# 0005 the dividend codes and two groups (v2.4), 0006 the cash movement codes (v2.5),
# 0007 the adjustment codes and their reasons (v2.6), 0008 the market master groups and match results (v2.7).
SEED: tuple[CodeGroup, ...] = (
    CodeGroup("ACCOUNT_STATUS", "계좌 상태", "위탁계좌의 상태. 정상 계좌만 주문을 받아요 (R-39).",
              "account_status_history.status", (
                  ("OPEN", "정상", "주문, 결제, 마감, 대사를 모두 해요"),
                  ("SUSPENDED", "거래정지", "새 주문만 막고 결제, 마감, 대사는 계속해요"),
                  ("CLOSED", "해지", "잔고가 0일 때만 가능하고 다시 열지 않아요"))),
    CodeGroup("ACCOUNT_ENV", "계좌 환경", "증권사 모의투자 계좌인지 실전 계좌인지.", "trading_accounts.env", (
        ("PAPER", "모의투자", "Phase 4 전까지는 모의투자만 등록해요"),
        ("LIVE", "실전", "실계좌. 이중 잠금을 풀어야 주문이 나가요"))),
    CodeGroup("ACCOUNT_TYPE", "계좌 유형", "위탁계좌의 종류.", "trading_accounts.account_type", (
        ("GENERAL", "위탁종합", "국내 주식·ETF 현물을 거래하는 일반 위탁계좌"),)),
    CodeGroup("BROKER", "증권사", "주문을 내고 잔고를 받아 오는 증권사.", "trading_accounts.broker", (
        ("KIS", "한국투자증권", "KIS Developers Open API"),)),
    CodeGroup("MARKET", "시장", "종목이 상장된 시장.", "instruments.market", (
        ("KOSPI", "유가증권시장", ""),
        ("KOSDAQ", "코스닥시장", ""))),
    CodeGroup("ASSET_TYPE", "자산 유형", "종목의 종류. 매도 세율이 여기서 갈려요.", "instruments.asset_type", (
        ("STOCK", "주식", "매도할 때 증권거래세와 농어촌특별세가 붙어요"),
        ("ETF", "상장지수펀드", "매도할 때 거래세가 없어요"))),
    CodeGroup("ORDER_SIDE", "매매 구분", "주문과 체결의 방향.", "oms_orders.side", (
        ("BUY", "매수", ""),
        ("SELL", "매도", ""))),
    CodeGroup("ORDER_ACK", "주문 접수 결과", "증권사가 주문을 받았는지.", "oms_order_acks.outcome", (
        ("ACCEPTED", "접수", "증권사 주문번호가 있어요"),
        ("REJECTED", "거부", "증권사가 받지 않았어요. 사유는 응답 내용에 남아요"))),
    CodeGroup("ORDER_STATUS", "주문 상태", "주문 하나가 거치는 상태 (주문·체결 화면).", None, (
        ("NEW", "신규", "주문 의도를 저장했고 아직 증권사에 내지 않았어요"),
        ("SUBMITTED", "접수", "증권사가 받았어요"),
        ("PARTIALLY_FILLED", "부분체결", ""),
        ("FILLED", "체결완료", ""),
        ("CANCEL_PENDING", "취소 중", "취소를 냈고 결과를 기다려요"),
        ("CANCELED", "취소", ""),
        ("REJECTED", "거부", ""),
        ("REPLACED", "정정", ""))),
    CodeGroup("ORDER_REJECT", "주문 전 검증 거부 사유", "주문을 내기 전에 시스템이 막는 이유 (R-36~39).", None, (
        ("KILL_SWITCH", "킬 스위치", "킬 스위치가 켜져 있어 모든 주문을 막아요"),
        ("ACCOUNT_NOT_OPEN", "정상 계좌 아님", "정지·해지 계좌는 주문을 받지 않아요 (R-39)"),
        ("NOT_BUSINESS_DAY", "영업일 아님", "휴장일에는 주문을 내지 않아요 (R-38)"),
        ("BAD_QTY", "수량 오류", "수량은 1주 이상"),
        ("BAD_TICK", "호가단위 오류", "가격이 호가단위에 맞지 않아요 (R-38)"),
        ("MAX_ORDER_VALUE", "1회 주문 한도 초과", ""),
        ("SHORT_SELL", "매도가능수량 초과", "공매도 금지 (R-36)"),
        ("INSUFFICIENT_CASH", "주문가능금액 부족", ""),
        ("NO_PRICE", "기준 단가 없음", "지정가도 최근 종가도 없어요"))),
    CodeGroup("RECON_STATUS", "대사 결과", "원장 값과 증권사 값을 맞춰 본 결과.", "recon_results.status", (
        ("MATCH", "일치", ""),
        ("BREAK", "불일치", "원인을 판단해 해결 행을 남겨야 무오류일이 돼요 (R-74)"),
        ("RESOLVED", "해결", "불일치 행을 고치지 않고 판단 행을 덧붙여요"))),
    CodeGroup("RECON_ITEM", "대사 항목", "무엇을 맞춰 보는지.", "recon_results.item", (
        ("CASH_D0", "예수금", "오늘 쓸 수 있는 예수금"),
        ("CASH_PROJECTED", "결제 후 예수금", "미결제 체결이 모두 결제된 뒤의 예수금"),
        ("POSITION_QTY", "보유수량", "종목별"),
        ("BOOK_COST", "장부금액", "종목별 매입금액"),
        ("EXEC_QTY", "체결수량", "장중 대사: 그날 체결된 수량"))),
    CodeGroup("RECON_CAUSE", "대사 불일치 원인", "불일치의 원인 분류 (R-73). 최종 판단은 사람이 해요.",
              "recon_results.cause_code", (
                  ("MISSING_EXEC", "체결 누락", "원장에 없는 체결이 증권사에 있어요"),
                  ("FEE_DIFF", "수수료 차이", ""),
                  ("TAX_DIFF", "세금 차이", ""),
                  ("ROUNDING", "단수 차이", "건별 계산과 합산 계산의 1원 단위 차이 (R-22)"),
                  ("CORP_ACTION", "권리 발생", "배당, 증자, 분할 같은 권리 반영 시점 차이"),
                  ("TIMING", "시점 차이", "조회 시점이 달라 생긴 차이"),
                  ("UNKNOWN", "원인 미상", "이 원인이 남아 있으면 무오류일이 아니에요"))),
    CodeGroup("SNAPSHOT_KIND", "증권사 응답 종류", "증권사 응답 원본을 받은 계기.", "broker_snapshots.kind", (
        ("OPENING_BALANCE", "시작 잔고", "원장을 열 때 받은 잔고"),
        ("DAILY_RECON", "일일 대사", "장 마감 뒤 대사에 쓴 잔고"),
        ("INTRADAY_RECON", "장중 대사", "장중에 체결을 맞춰 본 응답"),
        ("FILL_NOTICE", "체결통보", "실시간 체결통보 원본"),
        ("INTRADAY_PRICE", "장중 현재가", "보유·미체결 종목의 장중 현재가 (R-68)"),
        ("CANCEL_REQUEST", "취소 요청", "증권사가 받아들인 주문 취소 요청 (R-45)"))),
    CodeGroup("EVENT_TYPE", "원장 이벤트 유형", "원장에 쌓이는 이벤트의 종류. 모든 기록의 원천이에요.",
              "ledger_events.event_type", (
                  ("DepositEvent", "입금", "투자원금 입금"),
                  ("OpeningPositionEvent", "시작 보유", "원장을 열 때 이미 가지고 있던 종목"),
                  ("ExecutionEvent", "체결", "매수·매도 체결"),
                  ("SettlementEvent", "결제", "체결 대금이 예수금에 반영되는 날"),
                  ("RevaluationEvent", "평가", "종가로 다시 평가"),
                  ("NavEvent", "기준가", "하루의 기준가 산출"),
                  ("LateExecutionEvent", "마감 후 체결", "마감한 날의 체결을 열린 날에 정정으로 기록"),
                  ("DayClosedEvent", "일 마감", "이 날짜까지 장부를 마감"),
                  ("DividendAccrualEvent", "배당 인식", "받을 배당을 미수배당금으로 인식"),
                  ("DividendPaymentEvent", "배당 입금", "배당금이 세금을 떼고 예수금으로 들어옴"),
                  ("DividendCancelEvent", "배당 인식 취소", "취소된 권리의 인식을 되돌림"),
                  ("WithdrawalEvent", "출금", "투자원금 출금"),
                  ("TransferOutEvent", "대체 출금", "같은 고객의 다른 계좌로 보냄"),
                  ("TransferInEvent", "대체 입금", "같은 고객의 다른 계좌에서 받음"),
                  ("AdjustmentEvent", "수동 조정", "원장이 계산할 수 없는 예수금 변동을 사유와 함께 기록"),
                  ("AdjustmentCancelEvent", "수동 조정 취소", "조정을 반대 전표로 되돌림"))),
    CodeGroup("JOURNAL_KIND", "전표 유형", "분개 전표가 만들어진 이유.", "journal_entries.kind", (
        ("DEPOSIT", "입금", ""),
        ("OPENING", "시작 보유", ""),
        ("EXECUTION", "체결", "체결일에 미수금·미지급금으로 인식"),
        ("SETTLEMENT", "결제", "결제일에 예수금으로 정리"),
        ("VALUATION", "평가", "종가 평가손익"),
        ("VALUATION_REVERSAL", "평가 환입", "전일 평가분을 되돌림"),
        ("LATE_EXECUTION", "마감 후 체결", "마감한 날의 체결을 열린 날 전표로 인식 (정정)"),
        ("DIVIDEND_ACCRUAL", "배당 인식", "미수배당금과 배당수익으로 인식"),
        ("DIVIDEND", "배당 입금", "지급일에 예수금과 원천징수세액으로 정리"),
        ("DIVIDEND_CANCEL", "배당 인식 취소", "취소된 권리의 인식을 되돌림"),
        ("WITHDRAWAL", "출금", ""),
        ("TRANSFER_OUT", "대체 출금", "다른 계좌로 보낸 돈"),
        ("TRANSFER_IN", "대체 입금", "다른 계좌에서 받은 돈"),
        ("ADJUSTMENT", "수동 조정", "사유 코드가 정한 계정으로 기록"),
        ("ADJUSTMENT_CANCEL", "수동 조정 취소", "조정 전표의 반대 전표"))),
    CodeGroup("CASH_REASON", "예수금 변동 사유", "예수금이 움직인 이유. 예수금은 결제일에만 변해요 (R-13).",
              "cash_ledger.reason", (
                  ("DEPOSIT", "입금", ""),
                  ("SETTLEMENT", "결제", "매수 대금 지급 또는 매도 대금 수령"),
                  ("DIVIDEND", "배당 입금", "세금을 뗀 배당금 수령"),
                  ("WITHDRAWAL", "출금", ""),
                  ("TRANSFER_OUT", "대체 출금", "다른 계좌로 보낸 돈"),
                  ("TRANSFER_IN", "대체 입금", "다른 계좌에서 받은 돈"),
                  ("ADJUSTMENT", "수동 조정", "예탁금 이용료, 수수료·세금·배당 차이"),
                  ("ADJUSTMENT_CANCEL", "수동 조정 취소", "조정을 되돌림"))),
    CodeGroup("NORMAL_SIDE", "정상잔액 방향", "계정과목의 잔액이 늘어나는 쪽.", "accounts_chart.normal_side", (
        ("D", "차변", "자산, 비용"),
        ("C", "대변", "부채, 자본, 수익"))),
    CodeGroup("CORPORATE_ACTION_TYPE", "권리 유형", "종목에 생긴 권리의 종류.", "corporate_actions.action_type", (
        ("CASH_DIVIDEND", "현금배당", "주식 배당금과 ETF 분배금"),
        ("CANCEL", "취소", "앞서 등록한 권리를 무효로 하는 행"))),
    CodeGroup("DIVIDEND_STATUS", "배당 상태", "계좌에서 배당이 어디까지 진행됐는지.", "dividends_v.status", (
        ("ACCRUED", "인식", "미수배당금으로 잡혀 있고 아직 입금 전"),
        ("PAID", "입금", "배당금이 예수금으로 들어옴"),
        ("CANCELED", "취소", "권리가 취소돼 인식을 되돌림"))),
    CodeGroup("ADJUSTMENT_REASON", "수동 조정 사유", "조정하는 이유. 사유가 상대 계정을 정해요 (R-66).",
              "adjustments_v.reason", (
                  ("INTEREST", "예탁금 이용료", "증권사가 예수금에 준 이자. 상대 계정은 이자수익, 세금은 법인세등"),
                  ("FEE", "수수료 차이", "증권사가 실제로 뗀 수수료와 원장 계산의 차이. 상대 계정은 지급수수료"),
                  ("TAX", "거래세 차이", "증권사가 실제로 뗀 거래세와 원장 계산의 차이. 상대 계정은 세금과공과"),
                  ("DIVIDEND", "배당금 차이", "입금으로 기록한 배당금과 실제 금액의 차이. 상대 계정은 배당수익"),
                  ("WITHHOLDING", "원천징수 차이", "배당에서 실제로 뗀 세금과 기록한 세금의 차이. 상대 계정은 법인세등"),
                  ("OTHER", "기타", "위에 없는 사유. 상대 계정은 잡이익 또는 잡손실"))),
    CodeGroup("SECURITY_GROUP", "증권그룹 구분", "KIS 마스터 파일이 종목마다 붙이는 증권 종류.",
              "instrument_master.sec_group", tuple((code, name, "") for code, name in MASTER_GROUPS.items())),
    CodeGroup("MASTER_MATCH", "종목 마스터 대조 결과", "등록 종목이 시장 마스터와 같은지 (R-95).",
              "instrument_master_match_v.status", (
                  ("MATCH", "일치", "시장, 구분, 이름이 같음"),
                  ("RENAMED", "이름만 다름", "띄어쓰기 말고 이름이 다름. 종목명 변경일 수 있음"),
                  ("DIFFERENT", "시장·구분 다름", "등록할 때 시장이나 자산 유형을 잘못 넣었을 수 있음"),
                  ("DELISTED", "상장폐지", "마스터 파일에서 빠진 종목"),
                  ("MISSING", "마스터에 없음", "적재한 마스터에 한 번도 나온 적 없는 코드"))),
    CodeGroup("USER_RIGHT", "결재 권한", "사용자가 주문에 대해 할 수 있는 일 (R-99).", "user_rights.right_code", (
        ("ORDER_WRITE", "주문 작성", "주문 요청을 쓰고 결재를 요청함"),
        ("ORDER_APPROVE", "주문 승인", "요청을 승인·반려함. 1건 한도와 셀프 승인 허용을 함께 둠"),
        ("RIGHTS_ADMIN", "권한 관리", "사용자를 등록하고 권한을 주거나 거둠"))),
    CodeGroup("APPROVAL_ACTION", "결재 행위", "주문 요청에 일어난 일. 순서는 DB가 지켜요 (R-100).",
              "order_approvals.action", (
                  ("REQUEST", "결재 요청", "작성자가 요청을 올림"),
                  ("APPROVE", "승인", "승인자가 승인함. 작성자 본인이면 셀프 승인"),
                  ("REJECT", "반려", "승인자가 사유와 함께 돌려보냄"),
                  ("CANCEL", "요청 취소", "작성자가 전송 전에 거둠"),
                  ("EXPIRE", "만료", "주문일 장 마감까지 나가지 않음"))),
    CodeGroup("REQUEST_STATUS", "주문 요청 상태", "결재 이력의 마지막 행과 줄별 전송 결과로 정해지는 요청의 상태.",
              "order_requests_v.status", (
                  ("PENDING", "결재 대기", ""),
                  ("APPROVED", "승인", "전송 대기"),
                  ("REJECTED", "반려", ""),
                  ("CANCELED", "취소", ""),
                  ("SENT", "전송", "모든 줄을 처리했고 한 줄 이상 나감"),
                  ("SEND_FAILED", "전송 실패", "모든 줄이 전송 실패"),
                  ("EXPIRED", "만료", ""),
                  ("SEND_UNKNOWN", "결과 미확인", "보낸 줄 가운데 증권사 답이 없는 줄이 있음. 찾을 때까지 다음 전송을 멈춰요"))),
    CodeGroup("ORDER_TYPE", "주문 유형", "요청 한 건에 담긴 주문의 모양. 결재는 요청 단위로 해요 (R-99).",
              "order_requests.order_type", (
                  ("SINGLE", "단건", "종목 한 줄"),
                  ("BASKET", "바스켓", "종목 여러 줄을 한 번에 결재해요. 승인·반려·취소는 요청 전체로"))),
    CodeGroup("SEND_OUTCOME", "전송 결과", "승인된 요청의 줄마다 한 번 남는 전송 결과 (R-101).",
              "order_sends.outcome", (
                  ("SENT", "전송", "주문 경로로 보냄. 주문 ID가 붙어요"),
                  ("FAILED", "전송 실패", "보내기 직전 검증에 걸리거나 주문 경로가 거부함"))),
    CodeGroup("AUDIT_CHANNEL", "감사 로그 경로", "어디로 들어온 요청인지.", "audit_log.channel", (
        ("CLI", "명령", "PC의 명령 창에서 실행"),
        ("UI", "화면", "업무화면의 처리 버튼"),
        ("SYSTEM", "시스템", "사람이 아닌 프로그램 (샘플 생성 등)"))),
    CodeGroup("AUDIT_OUTCOME", "감사 로그 결과", "요청이 어떻게 끝났는지.", "audit_log.outcome", (
        ("DONE", "처리", "요청대로 처리함"),
        ("REFUSED", "거부", "규칙에 맞지 않아 시스템이 거부함. 바뀐 것은 없어요"),
        ("FAILED", "실패", "증권사 응답 등 바깥 문제로 끝내지 못함"))),
    CodeGroup("AUDIT_ACTION", "감사 로그 행위", "밖에서 시스템에 한 일의 종류.", "audit_log.action", (
        ("OPEN", "원장 시작", "증권사 잔고를 원장 시작값으로 기록"),
        ("ORDER", "주문", "주문 한 건. 검증에서 거부된 주문도 남아요"),
        ("ORDER_CANCEL", "주문 취소", "미체결 잔량 취소 요청 (R-45)"),
        ("DAILY", "일일 마감·대사", "결제, 체결 반영, 기준가, 대사"),
        ("CLOSE", "장부 마감", "불일치를 판단한 날을 직접 마감"),
        ("RESOLVE", "불일치 원인 판단", ""),
        ("INSTRUMENT_ADD", "종목 등록", ""),
        ("CUSTOMER_ADD", "고객 등록", ""),
        ("ACCOUNT_ADD", "계좌 등록", ""),
        ("ACCOUNT_STATUS", "계좌 상태 변경", ""),
        ("CODE_GROUP_ADD", "코드 그룹 추가", ""),
        ("CODE_ADD", "코드 추가", ""),
        ("CODE_RENAME", "코드명 변경", ""),
        ("CODE_USE", "코드 사용 여부 변경", ""),
        ("DB_MIGRATE", "스키마 마이그레이션", "ERD 버전을 올림"),
        ("CORPORATE_ACTION_ADD", "권리 등록", "현금배당 일정과 주당 배당금 등록"),
        ("CORPORATE_ACTION_CANCEL", "권리 취소", "잘못 등록했거나 취소된 권리를 무효로"),
        ("DIVIDEND_PAY", "배당 입금 기록", "증권사가 실제로 준 금액으로 직접 기록"),
        ("CASH_DEPOSIT", "입금 기록", "증권사 계좌에 들어온 돈"),
        ("CASH_WITHDRAW", "출금 기록", "증권사 계좌에서 나간 돈"),
        ("CASH_TRANSFER", "계좌 간 대체", "같은 고객의 두 계좌 사이로 옮긴 돈"),
        ("ADJUSTMENT_ADD", "수동 조정", "예탁금 이용료나 증권사와의 차이를 사유와 함께 기록"),
        ("ADJUSTMENT_CANCEL", "수동 조정 취소", "조정을 반대 전표로 되돌림"),
        ("MASTER_LOAD", "종목 마스터 적재", "받은 마스터 파일에서 바뀐 종목만 이력으로 넣음"),
        ("USER_ADD", "사용자 등록", ""),
        ("RIGHT_GRANT", "권한 부여", "결재 권한을 주거나 한도를 바꿈"),
        ("RIGHT_REVOKE", "권한 회수", ""),
        ("ORDER_REQUEST", "주문 결재 요청", "묶음 검증(R-98)을 통과한 요청만 남아요"),
        ("ORDER_APPROVE", "주문 승인", "셀프 승인 포함"),
        ("ORDER_REJECT", "주문 반려", ""),
        ("ORDER_REQUEST_CANCEL", "주문 요청 취소", "작성자가 전송 전에 거둠"),
        ("ORDER_CONFIRM", "주문 결과 확인", "답이 없던 주문을 사람이 증권사 주문내역과 맞춰 확인함"))),
)


def seed_rows() -> tuple[list[tuple], list[tuple]]:
    """(group rows, code rows) of the seed, in the column order of the two tables."""
    groups = [(g.group_id, g.name, g.description, g.used_by, g.fixed) for g in SEED]
    codes = [(g.group_id, code, name, desc or None, i)
             for g in SEED for i, (code, name, desc) in enumerate(g.codes, 1)]
    return groups, codes


def seed(conn: psycopg.Connection) -> None:
    """Insert the seed into a new database."""
    groups, codes = seed_rows()
    with conn.cursor() as cur:
        cur.executemany("insert into code_groups (group_id, name, description, used_by, fixed)"
                        " values (%s, %s, %s, %s, %s)", groups)
        cur.executemany("insert into common_codes (group_id, code, name, description, sort_order)"
                        " values (%s, %s, %s, %s, %s)", codes)


def seed_sql() -> str:
    """The same seed as SQL. The migrations' inserts were written from it; a test compares the two results."""
    def lit(v) -> str:
        if v is None:
            return "null"
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, int):
            return str(v)
        return "'" + str(v).replace("'", "''") + "'"
    groups, codes = seed_rows()
    out = ["insert into code_groups (group_id, name, description, used_by, fixed) values"]
    out.append(",\n".join("    (" + ", ".join(lit(v) for v in g) + ")" for g in groups) + ";")
    out.append("insert into common_codes (group_id, code, name, description, sort_order) values")
    out.append(",\n".join("    (" + ", ".join(lit(v) for v in c) + ")" for c in codes) + ";")
    return "\n".join(out) + "\n"


def program_codes() -> dict[str, set[str]]:
    """The codes the program itself defines, by group. `check()` holds the table to them."""
    from . import approval
    from .accounts import STATUSES
    from .instruments import AssetType, Market
    from .ledger import ADJUSTMENT_ACCOUNTS, DIVIDEND_STATUSES, Side
    from .master_db import MATCH_STATUSES
    from .rights import ACTION_TYPES
    from .oms import OrderStatus
    from .recon import Cause, Status
    from .risk import REASON_TEXT
    from .store.postgres import EVENT_TYPES
    return {
        "ACCOUNT_STATUS": set(STATUSES),
        "MARKET": {m.value for m in Market},
        "ASSET_TYPE": {a.value for a in AssetType},
        "EVENT_TYPE": set(EVENT_TYPES),
        "ORDER_SIDE": {s.value for s in Side},
        "ORDER_STATUS": {s.value for s in OrderStatus},
        "ORDER_REJECT": {r for r in REASON_TEXT if r},
        "RECON_STATUS": {s.value for s in Status} | {"RESOLVED"},   # RESOLVED is written by the judgment row
        "RECON_CAUSE": {c.value for c in Cause},
        "AUDIT_CHANNEL": set(audit.CHANNELS),
        "AUDIT_OUTCOME": set(audit.OUTCOMES),
        "AUDIT_ACTION": set(audit.ACTIONS),
        "CORPORATE_ACTION_TYPE": set(ACTION_TYPES),
        "DIVIDEND_STATUS": set(DIVIDEND_STATUSES),
        "ADJUSTMENT_REASON": set(ADJUSTMENT_ACCOUNTS),
        "SECURITY_GROUP": set(MASTER_GROUPS),
        "MASTER_MATCH": set(MATCH_STATUSES),
        "USER_RIGHT": set(approval.RIGHTS),
        "APPROVAL_ACTION": set(approval.ACTIONS),
        "REQUEST_STATUS": set(approval.STATUSES),
        "ORDER_TYPE": set(approval.ORDER_TYPES),
        "SEND_OUTCOME": set(approval.SEND_OUTCOMES),
    }


# ---------- reading ----------

def list_groups(conn: psycopg.Connection) -> list[tuple]:
    """(group_id, name, description, used_by, fixed, codes in use, codes switched off)."""
    return conn.execute(
        "select g.group_id, g.name, g.description, g.used_by, g.fixed,"
        " count(c.code) filter (where c.in_use), count(c.code) filter (where not c.in_use)"
        " from code_groups g left join common_codes c using (group_id)"
        " group by g.group_id order by g.group_id").fetchall()


def list_codes(conn: psycopg.Connection, group_id: str) -> list[tuple]:
    """(code, name, description, in_use) of one group, in display order."""
    _group(conn, group_id)
    return conn.execute("select code, name, description, in_use from common_codes where group_id = %s"
                        " order by sort_order, code", (group_id,)).fetchall()


def names(conn: psycopg.Connection, group_id: str) -> dict[str, str]:
    """code -> name of one group, for showing a code with its name."""
    return dict(conn.execute("select code, name from common_codes where group_id = %s", (group_id,)).fetchall())


def _group(conn: psycopg.Connection, group_id: str) -> tuple:
    row = conn.execute("select group_id, name, fixed from code_groups where group_id = %s", (group_id,)).fetchone()
    if row is None:
        raise OpsError(f"없는 코드 그룹이에요: {group_id}. code list로 확인해 주세요.")
    return row


def _code(conn: psycopg.Connection, group_id: str, code: str) -> tuple:
    _group(conn, group_id)
    row = conn.execute("select code, name, in_use from common_codes where group_id = %s and code = %s",
                       (group_id, code)).fetchone()
    if row is None:
        raise OpsError(f"{group_id}에 없는 코드예요: {code}. code list {group_id}로 확인해 주세요.")
    return row


# ---------- changing (the console only; the screens read) ----------

def rename(conn: psycopg.Connection, group_id: str, code: str, name: str, description: str | None = None) -> str:
    """Change a code's name (and its description when one is given). Returns the old name."""
    old = _code(conn, group_id, code)[1]
    if not name.strip():
        raise OpsError("코드명을 넣어 주세요.")
    with conn.transaction():
        conn.execute("update common_codes set name = %s, description = coalesce(%s, description)"
                     " where group_id = %s and code = %s", (name.strip(), description, group_id, code))
        # common_codes is not append-only (a name is a label, not a fact): the old name is kept here
        audit.record(conn, "CODE_RENAME", target=f"{group_id}.{code}",
                     detail={"from": old, "to": name.strip(), "description": description})
    return old


def set_in_use(conn: psycopg.Connection, group_id: str, code: str, in_use: bool) -> None:
    """Switch a code off (or back on). A code that old rows carry is never deleted."""
    _, _, fixed = _group(conn, group_id)
    _code(conn, group_id, code)
    if fixed:
        raise OpsError(f"{group_id}는 프로그램이 정한 코드라서 끌 수 없어요. 코드명만 바꿀 수 있어요 (code rename).")
    with conn.transaction():
        conn.execute("update common_codes set in_use = %s where group_id = %s and code = %s", (in_use, group_id, code))
        audit.record(conn, "CODE_USE", target=f"{group_id}.{code}", detail={"in_use": in_use})


def add_group(conn: psycopg.Connection, group_id: str, name: str, description: str = "") -> None:
    """A group the operator owns: its codes can be added and switched off from the console."""
    if not GROUP_ID_RE.match(group_id):
        raise OpsError(f"코드 그룹 ID는 영문 대문자로 시작하는 대문자·숫자·밑줄이에요 (예: MEMO_KIND). 받은 값: {group_id}")
    if not name.strip():
        raise OpsError("코드 그룹명을 넣어 주세요.")
    if conn.execute("select 1 from code_groups where group_id = %s", (group_id,)).fetchone():
        raise OpsError(f"이미 있는 코드 그룹이에요: {group_id}")
    with conn.transaction():
        conn.execute("insert into code_groups (group_id, name, description, fixed) values (%s, %s, %s, false)",
                     (group_id, name.strip(), description.strip() or None))
        audit.record(conn, "CODE_GROUP_ADD", target=group_id,
                     detail={"name": name.strip(), "description": description.strip() or None})


def add_code(conn: psycopg.Connection, group_id: str, code: str, name: str, description: str = "") -> None:
    _, _, fixed = _group(conn, group_id)
    if fixed:
        raise OpsError(f"{group_id}는 프로그램이 정한 코드라서 여기서 코드를 늘릴 수 없어요. "
                       "프로그램과 스키마를 함께 바꾸는 변경이에요 (마이그레이션).")
    if not code.strip() or not name.strip():
        raise OpsError("코드와 코드명을 넣어 주세요.")
    if conn.execute("select 1 from common_codes where group_id = %s and code = %s", (group_id, code)).fetchone():
        raise OpsError(f"이미 있는 코드예요: {group_id} {code}")
    last = conn.execute("select coalesce(max(sort_order), 0) from common_codes where group_id = %s",
                        (group_id,)).fetchone()[0]
    with conn.transaction():
        conn.execute("insert into common_codes (group_id, code, name, description, sort_order) values (%s, %s, %s, %s, %s)",
                     (group_id, code.strip(), name.strip(), description.strip() or None, last + 1))
        audit.record(conn, "CODE_ADD", target=f"{group_id}.{code.strip()}",
                     detail={"name": name.strip(), "description": description.strip() or None})


# ---------- the check ----------

_LITERAL = re.compile(r"'((?:[^']|'')*)'::")


def _check_list(conn: psycopg.Connection, table: str, column: str) -> set[str] | None:
    """The values a CHECK constraint allows for one column, or None when it has no such list."""
    rows = conn.execute(
        "select pg_get_constraintdef(c.oid) from pg_constraint c"
        " join pg_class t on t.oid = c.conrelid join pg_namespace n on n.oid = t.relnamespace"
        " join pg_attribute a on a.attrelid = t.oid and a.attnum = any (c.conkey)"
        " where c.contype = 'c' and n.nspname = current_schema() and t.relname = %s and a.attname = %s"
        "   and cardinality(c.conkey) = 1", (table, column)).fetchall()
    for (definition,) in rows:      # e.g. CHECK ((status = ANY (ARRAY['OPEN'::text, 'CLOSED'::text])))
        found = {m.replace("''", "'") for m in _LITERAL.findall(definition)}
        if found and "~" not in definition and "<>" not in definition:   # a pattern or "not empty" is no code list
            return found
    return None


# Columns that store the codes of a group whose `used_by` names another column: checked the same way.
SAME_CODES = {"instrument_master.market": "MARKET", "order_request_lines.side": "ORDER_SIDE"}


def check(conn: psycopg.Connection) -> list[str]:
    """Where the code table has drifted from the schema, the program or the stored data. Empty when it has not."""
    problems = []
    groups = {g[0]: g for g in conn.execute("select group_id, name, used_by, fixed from code_groups").fetchall()}
    codes: dict[str, set[str]] = {g: set() for g in groups}
    for g, code in conn.execute("select group_id, code from common_codes").fetchall():
        codes[g].add(code)

    def diff(gid: str, where: str, expected: set[str]) -> None:
        missing, extra = sorted(expected - codes[gid]), sorted(codes[gid] - expected)
        if missing:
            problems.append(f"{gid}: {where}에는 있는데 공통코드에 없는 코드 {', '.join(missing)}")
        if extra and groups[gid][3]:
            problems.append(f"{gid}: 공통코드에는 있는데 {where}에 없는 코드 {', '.join(extra)}")

    for gid, expected in program_codes().items():
        if gid not in groups:
            problems.append(f"{gid}: 프로그램이 쓰는 코드 그룹인데 공통코드에 없어요")
        else:
            diff(gid, "프로그램", expected)
    where = [(gid, used_by) for gid, (_, _, used_by, _) in sorted(groups.items()) if used_by]
    where += [(gid, col) for col, gid in sorted(SAME_CODES.items()) if gid in groups]
    for gid, used_by in where:
        table, _, column = used_by.partition(".")
        exists = conn.execute(
            "select 1 from information_schema.columns where table_schema = current_schema()"
            " and table_name = %s and column_name = %s", (table, column)).fetchone()
        if not exists:
            problems.append(f"{gid}: 쓰는 곳 {used_by}이 스키마에 없어요")
            continue
        allowed = _check_list(conn, table, column)
        if allowed is not None:
            diff(gid, f"스키마 제약({used_by})", allowed)
        stored = {r[0] for r in conn.execute(
            sql.SQL("select distinct {}::text from {} where {} is not null").format(
                sql.Identifier(column), sql.Identifier(table), sql.Identifier(column)))}
        unnamed = sorted(stored - codes[gid])
        if unnamed:
            problems.append(f"{gid}: {used_by}에 저장돼 있는데 코드명이 없는 값 {', '.join(unnamed)}")
    return problems
