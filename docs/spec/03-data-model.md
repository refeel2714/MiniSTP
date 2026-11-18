# 03. 데이터 모델

공통 규칙
- 모든 테이블은 `id`(bigserial), `created_at`(timestamptz)을 가진다.
- `*_ledger`, `*_events`, `journal_*`, `recon_results`, `broker_snapshots`는 append-only다. UPDATE와 DELETE를 막는 트리거를 둔다.
- 금액 컬럼은 `bigint`(원), 단가 표시값만 `numeric(18,2)`.

## 기준 정보

| 테이블 | 주요 컬럼 | 비고 |
|---|---|---|
| instruments | symbol, name, market(KOSPI/KOSDAQ), asset_type(STOCK/ETF), listed_at, delisted_at | 종목 마스터 |
| market_calendar | date, is_business_day, note | R-10 |
| fee_schedule | effective_from, item(COMMISSION/TX_TAX/RURAL_TAX/DIV_TAX), market, asset_type, side, rate | R-20~23 |
| accounts_chart | code, name, category, normal_side | D4 계정과목 |

## 주문과 체결

| 테이블 | 주요 컬럼 | 비고 |
|---|---|---|
| orders | order_id, client_order_id, broker_order_no, orig_order_id, symbol, side, order_type, qty, limit_price, strategy_id, mode(BACKTEST/PAPER/LIVE) | 생성 정보만, 상태는 이벤트로 |
| order_events | order_id, event_type, status_after, payload(jsonb), occurred_at | R-40~43 |
| executions | exec_id, order_id, broker_exec_no, symbol, side, qty, price, gross_amount, executed_at, trade_date | 증권사 체결번호 unique |

Phase 2 구현(v1.4)은 위 설계를 단순화했다. 주문 생성 정보는 `oms_orders`(client_id, trade_date, symbol, side, qty, limit_price), 증권사 접수 결과는 `oms_order_acks`(client_id, outcome, order_no, org_no, detail)에 저장한다(R-44). 체결은 `ledger_events`의 ExecutionEvent(exec_id = `주문번호-순번`)와 `position_ledger`로 남는다. 종목 정보는 `instruments`(symbol, name, market, asset_type)에 등록한다.

## 원장

| 테이블 | 주요 컬럼 | 비고 |
|---|---|---|
| settlements | exec_id, trade_date, settle_date, gross_amount, commission, tx_tax, rural_tax, net_amount, direction(PAY/RECEIVE) | 예정 행과 완료 이벤트(`settlement_events`)를 분리 |
| settlement_events | settlement_id, status(SETTLED/FAILED), occurred_at | |
| cash_ledger | date, reason(SETTLEMENT/DEPOSIT/WITHDRAWAL/DIVIDEND/ADJUSTMENT), amount, ref_type, ref_id | 예수금 변동만 |
| position_ledger | trade_date, symbol, qty_delta, cost_delta, realized_pnl, ref_exec_id | R-30~34 |
| corporate_actions | symbol, type(CASH_DIVIDEND/MANUAL_ADJ), record_date, pay_date, amount_per_share | |

현재 잔고는 뷰로 계산한다: `positions_v`(종목별 Σqty_delta, Σcost_delta), `cash_balance_v`(Σamount).

## 회계와 성과

| 테이블 | 주요 컬럼 | 비고 |
|---|---|---|
| journal_entries | entry_id, date, description, source_type, source_id, reverses_entry_id | 분개 헤더 |
| journal_lines | entry_id, account_code, debit, credit | entry별 Σdebit = Σcredit (지연 제약 트리거) |
| daily_prices | date, symbol, close | 평가용 종가 |
| daily_nav | date, cash, receivables, payables, market_value, net_assets, units, nav_per_1000 | R-60~62 |
| unit_events | date, reason, units_delta, nav_used | 좌수 변동 |

## 대사

| 테이블 | 주요 컬럼 | 비고 |
|---|---|---|
| broker_snapshots | snapshot_id, date, kind(OPENING_BALANCE/DAILY_RECON), payload(jsonb), taken_at | 증권사 응답 원본. 대사 실행 1회 = 1행 |
| recon_results | run_id(→broker_snapshots), date, item, key(종목 등), internal_value, broker_value, diff, status(MATCH/BREAK/RESOLVED), cause_code, note, supersedes_id | R-70~74 |

## 불변 조건 (게이트 테스트로 상시 검증)

1. `cash_balance_v` = 예수금(1010) 계정 잔액
2. 종목별 `positions_v.cost` 합계 = FVPL 주식(1100) 계정 잔액
3. 미결제 settlements 합계 = 매도미수금(1020) − 매수미지급금(2010) 잔액
4. 모든 journal_entries는 차대변이 일치한다
5. 원장 전체를 이벤트에서 다시 재생하면 현재 잔고와 같다
