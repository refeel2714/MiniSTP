# STP-UI 화면 목록과 영향도

> `python scripts/gen_screen_docs.py`로 생성돼요. 화면 정의(YAML)와 DB 주석에서 만들어지니 직접 고치지 마세요.

## 화면

| 화면번호 | 화면명 | 그리드 | 트랜잭션 |
|---|---|---|---|
| [STP-1100](STP-1100.md) | 실시간 잔고 | 2 | `STP1100R02`, `STP1100R01` |
| [STP-1200](STP-1200.md) | 주문·체결 블로터 | 1 | `STP1200R01` |
| [STP-2100](STP-2100.md) | 일일 대사 결과 | 1 | `STP2100R01` |
| [STP-2200](STP-2200.md) | 기준가·무오류 현황 | 1 | `STP2200R01` |
| [STP-3100](STP-3100.md) | 원장·분개 조회 | 5 | `STP3100R01`, `STP3100R02`, `STP3100R03` |

## 영향도: DB 컬럼 → 화면

스키마를 바꾸기 전에 이 표에서 그 컬럼을 쓰는 화면을 확인해요.

| DB 컬럼 | 한글명 | 쓰는 화면·그리드 |
|---|---|---|
| `account_balances_v.balance` | 잔액 | STP-3100 trial |
| `accounts_chart.code` | 계정코드 | STP-3100 trial |
| `accounts_chart.name` | 계정명 | STP-3100 journal, STP-3100 trial |
| `accounts_chart.normal_side` | 정상잔액 방향 | STP-3100 trial |
| `broker_snapshots.kind` | 유형 | STP-2100 조회조건 |
| `broker_snapshots.taken_at` | 수신 시각 | STP-2100 results |
| `cash_ledger.amount` | 금액 | STP-3100 cash |
| `cash_ledger.date` | 일자 | STP-3100 cash |
| `cash_ledger.reason` | 사유 | STP-3100 cash |
| `cash_ledger.ref_id` | 참조 ID | STP-3100 cash |
| `instruments.asset_type` | 자산 유형 | STP-1100 holdings |
| `instruments.name` | 종목명 | STP-1100 holdings, STP-1200 orders |
| `instruments.symbol` | 종목코드 | STP-1100 holdings, STP-3100 events, STP-3100 조회조건 |
| `journal_entries.date` | 회계일 | STP-3100 journal |
| `journal_entries.entry_id` | 전표 번호 | STP-3100 journal |
| `journal_entries.kind` | 전표 유형 | STP-3100 journal |
| `journal_entries.reverses_entry_id` | 역분개 대상 전표 번호 | STP-3100 journal |
| `journal_entries.source_id` | 원천 참조 | STP-3100 journal |
| `journal_lines.account_code` | 계정코드 | STP-3100 journal |
| `journal_lines.credit` | 대변 금액 | STP-3100 journal |
| `journal_lines.debit` | 차변 금액 | STP-3100 journal |
| `ledger_events.event_type` | 이벤트 유형 | STP-3100 events, STP-3100 조회조건 |
| `ledger_events.payload` | 이벤트 내용 | STP-3100 events |
| `ledger_events.recorded_at` | 기록 시각 | STP-3100 events |
| `ledger_events.seq` | 이벤트 순번 | STP-3100 events |
| `oms_order_acks.detail` | 거부 사유 | STP-1200 orders |
| `oms_order_acks.order_no` | 증권사 주문번호 | STP-1200 orders |
| `oms_order_acks.outcome` | 결과 | STP-1200 orders |
| `oms_orders.client_id` | 내부 주문번호 | STP-1200 orders |
| `oms_orders.created_at` | 생성 시각 | STP-1200 orders |
| `oms_orders.limit_price` | 지정가 | STP-1200 orders |
| `oms_orders.qty` | 주문수량 | STP-1200 orders |
| `oms_orders.side` | 매매구분 | STP-1200 orders |
| `oms_orders.symbol` | 종목코드 | STP-1200 orders |
| `oms_orders.trade_date` | 주문일 | STP-1200 조회조건 |
| `position_ledger.cost_delta` | 장부금액 증감 | STP-3100 positions |
| `position_ledger.exec_id` | 체결 ID | STP-3100 positions |
| `position_ledger.qty_delta` | 수량 증감 | STP-3100 positions |
| `position_ledger.realized_pnl` | 실현손익 | STP-3100 positions |
| `position_ledger.symbol` | 종목코드 | STP-3100 positions |
| `position_ledger.trade_date` | 매매일 | STP-3100 positions |
| `positions_v.book_cost` | 장부금액 | STP-1100 holdings |
| `positions_v.qty` | 보유수량 | STP-1100 holdings |
| `recon_results.broker_value` | 증권사 값 | STP-2100 results |
| `recon_results.cause_code` | 원인 코드 | STP-2100 results |
| `recon_results.date` | 기준일 | STP-2100 results |
| `recon_results.diff` | 차이 | STP-2100 results |
| `recon_results.id` | 행 번호 | STP-2100 results |
| `recon_results.internal_value` | 내부(원장) 값 | STP-2100 results |
| `recon_results.item` | 대사 항목 | STP-2100 results |
| `recon_results.key` | 항목 키 | STP-2100 results |
| `recon_results.status` | 상태 | STP-2100 results |
