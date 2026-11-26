# 03. 데이터 모델


> 실제 구현된 테이블·컬럼과 한글 명칭은 [ERD와 데이터 사전](../erd/ERD.md)이 기준이에요. 이 문서는 설계 의도를 설명해요.

공통 규칙
- 모든 테이블은 `id`(bigserial), `created_at`(timestamptz)을 가진다.
- `*_ledger`, `*_events`, `journal_*`, `recon_results`, `broker_snapshots`와 고객·계좌 테이블(v1.12)은 append-only다. UPDATE와 DELETE를 막는 트리거를 둔다.
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

Phase 2 구현(v1.4)은 위 설계를 단순화했다. 주문 생성 정보는 `oms_orders`(client_id, trade_date, symbol, side, qty, limit_price), 증권사 접수 결과는 `oms_order_acks`(client_id, outcome, order_no, org_no, detail)에 저장한다(R-44). 체결은 `ledger_events`의 ExecutionEvent(exec_id = `YYYYMMDD-주문번호-순번`, 증권사 주문번호가 영업일마다 다시 시작될 수 있어 날짜를 붙임)와 `position_ledger`로 남는다. 종목 정보는 `instruments`(symbol, name, market, asset_type)에 등록한다.

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

## 스키마 변경 규칙 (v1.9)

새 DB는 `schema.sql`로 만들고(`db-init`), 이미 운영 중인 DB는 마이그레이션 파일(`src/ministp/store/migrations/NNNN_이름.sql`)로 다음 ERD 버전에 올린다(`db-migrate`).

1. 마이그레이션 하나는 트랜잭션 하나다. 실패하면 아무것도 남지 않는다.
2. 적용 전후로 모든 테이블의 지문(행 수, 기존 컬럼 기준 행 해시)을 비교한다. 기존 행이 하나라도 바뀌면 되돌린다. 의도한 변경은 파일 머리에 `-- changes-data: 테이블`로 적어야만 통과한다.
3. append-only 테이블의 기존 행은 적어 두어도 고칠 수 없다(R-04 트리거가 그대로 적용된다).
4. `--dry-run`은 적용과 검증을 모두 한 뒤 되돌린다. 운영 DB에 적용하기 전에 백업을 복원한 복사본에서 먼저 돌린다.
5. 적용된 마이그레이션은 번호·이름·해시로 `schema_migrations`에 기록한다. 적용된 파일을 고치면 거부한다.
6. ERD v1.1이 기준(0번)이다. `schema_migrations` 테이블은 첫 마이그레이션을 적용할 때 만들어지며, 그 전에는 상태를 조회해도 DB가 바뀌지 않는다.
7. (v1.12) `db-init`은 새 DB만 만든다. 새 DB는 `schema.sql`로 최신 ERD 버전에 바로 만들어지고, 그동안의 마이그레이션은 적용된 것으로 기록된다. 이미 원장이 있는 DB에서는 아무것도 하지 않으며, 남은 마이그레이션이 있으면 `db-migrate`를 안내하고 끝난다.
8. (v1.12) 코드가 DB보다 앞서 있으면(남은 마이그레이션이 있으면) 원장을 읽거나 쓰는 모든 명령은 시작하지 않는다. 모양이 다른 원장을 짐작해서 다루지 않는다.
9. (v1.12) `schema.sql`로 만든 새 DB와 마이그레이션으로 올린 기존 DB는 스키마가 같아야 한다(컬럼 순서, 제약 이름, 인덱스, 트리거, 뷰, 한글 주석까지). v1.1 DB의 덤프(`tests/db/fixtures/sample_v1_1.sql`)에 마이그레이션을 적용해 새 DB와 비교하는 테스트가 이를 강제한다.
10. (v1.12) 리허설은 지문 비교에 더해, 적용한 상태에서 원장 무결성 점검(아래)을 실행한다. 하나라도 어긋나면 실패다. `--apply`는 항상 리허설을 먼저 하고, 통과하지 못하면 아무것도 적용하지 않는다.

## 다계좌 (v1.12, ERD v2.0, 스펙 D7)

원장은 하나이고 이벤트 순번도 하나다. 모든 이벤트와 거기서 나온 행은 계좌 하나에 속한다.

| 테이블 | 주요 컬럼 | 비고 |
|---|---|---|
| customers | customer_id(`C0001` 형식), alias | 고객 1 : 계좌 N. 실명 등 개인정보는 두지 않는다 |
| trading_accounts | account_id(`A0001` 형식), customer_id, alias, broker(KIS), env(PAPER/LIVE), account_type(GENERAL), credential_ref, opened_on | 계좌번호와 인증키는 두지 않는다. `credential_ref`는 PC 환경변수 묶음의 이름표일 뿐이고, 같은 증권사·환경에서 한 계좌만 쓸 수 있다 |
| account_status_history | account_id, status(OPEN/SUSPENDED/CLOSED), effective_from, reason | 상태는 행을 고치지 않고 이력을 쌓는다. 현재 상태는 `account_status_v` |

1. `ledger_events`, `journal_entries`, `position_ledger`, `cash_ledger`, `oms_orders`, `broker_snapshots`, `recon_results`에 `account_id`가 있다.
2. 분개·보유·현금 행은 `(event_seq, account_id)`로 이벤트를 참조한다. 그래서 다른 계좌의 이벤트에 매달린 행은 DB가 받지 않는다. 대사 결과도 `(run_id, account_id)`로 같은 계좌의 증권사 응답만 참조한다.
3. 순번(`seq`)은 계좌와 상관없이 1부터 빈 번호 없이 이어진다. 계좌 하나의 원장은 그 계좌의 이벤트만 순서대로 재생한 결과다.
4. 저장 계층은 계좌 하나에 묶여 열린다. 이벤트를 쓰기 전에 그 계좌의 저장된 이벤트 수가 메모리의 원장과 같은지 확인하고, 다르면 쓰지 않고 원장을 멈춘다.
5. 체결 ID는 계좌 안에서만 유일하다(`(account_id, exec_id)`). 증권사 주문번호는 계좌마다 따로 매겨지기 때문이다.
6. 뷰 `account_balances_v`, `positions_v`, `cash_balance_v`는 계좌별이다. `customer_positions_v`는 한 고객의 계좌들을 종목별로 합친다.
7. 기존 DB의 원장은 마이그레이션 `0001_multi_account`가 기본 고객 `C0001`의 기본 계좌 `A0001`로 옮긴다. 개설일은 첫 이벤트의 날짜다. 기존 행의 기존 컬럼 값은 바뀌지 않는다.
8. 명령은 계좌 하나를 골라 실행한다(`--account`, 기본은 `A0001`). 화면은 아직 기본 계좌만 보여준다(다음 PR).

### 계좌 운영 (v1.13)

1. 고객과 계좌는 명령으로 등록한다(`customer add`, `account add`). 계좌를 등록하면 `OPEN` 상태 행이 함께 생긴다. Phase 4 전에는 모의투자(PAPER) 계좌만 등록한다.
2. 계좌의 인증정보는 `credential_ref`를 머리말로 하는 환경변수에서만 읽는다. `KIS_2`이면 `MINISTP_KIS_2_APP_KEY`, `MINISTP_KIS_2_APP_SECRET`, `MINISTP_KIS_2_ACCOUNT`, `MINISTP_KIS_2_HTS_ID`. 값이 없으면 실행할 때 물어보고 저장하지 않는다. 다른 계좌의 값으로 대신하지 않는다.
3. `credential_ref`는 계좌마다 달라야 한다(DB 유일 제약). 두 계좌의 환경변수가 같은 증권사 계좌번호를 가리키면 명령을 시작하지 않는다. 접근 토큰 캐시 파일도 계좌마다 따로 둔다(토큰은 앱키 하나에 속한다).
4. `open`, `run`, `order`, `daily`, `status`는 고른 계좌의 원장만 읽고 쓴다. 마감, 대사, 무오류 연속 영업일(R-74)도 계좌별이다.
5. 주문 ID(`oms_orders.client_id`)는 DB 전체에서 유일하다. 기본 계좌는 `S날짜-순번`, 다른 계좌는 `계좌ID-S날짜-순번`이다.
6. 원장을 쓰는 명령은 계좌와 상관없이 한 번에 하나만 실행된다(쓰기 잠금은 DB 하나에 하나). 여러 계좌의 `run`을 동시에 띄우는 것은 아직 지원하지 않는다.
7. 계좌 상태에 따른 주문 거부는 R-39를 따른다.

## 원장 무결성 점검 (v1.11)

`python -m ministp verify`는 저장된 원장이 이벤트를 처음부터 다시 계산한 결과와 같은지 읽기 전용으로 확인한다. 쓰기 잠금을 잡지 않으므로 `run`이 떠 있어도 실행할 수 있다. 하나라도 어긋나면 종료 코드 1을 돌려준다.

| 점검 | 확인하는 것 |
|---|---|
| 이벤트 순번 | `ledger_events.seq`가 1부터 빈 번호 없이 이어진다 |
| 수정·삭제 방지 | append-only 테이블 13개의 수정·삭제·비우기 금지 트리거와 분개 대차 트리거가 켜져 있다 (R-04, R-50) |
| 분개 대차 | 모든 분개의 차변 합 = 대변 합 |
| 분개 재생 | 저장된 분개가 이벤트 재생 결과와 건별로 같다 |
| 계정 잔액 | 계정과목별 잔액이 재생 결과와 같다 |
| 보유 | `position_ledger`의 행과 종목별 수량·장부금액이 재생 결과와 같다 |
| 현금 | `cash_ledger`의 행과 예수금이 재생 결과와 같다 |

아래 네 가지(재생 비교)는 계좌마다 따로 확인한다(v1.12). 모든 명령이 시작할 때도 확인하며, 어긋나면 그 명령은 실행되지 않는다. 코드가 DB보다 앞서 있을 때는 `verify`도 시작하지 않는다. 대신 `db-migrate --dry-run`이 마이그레이션을 적용한 상태에서 같은 점검을 한 뒤 되돌리고, `--apply`는 그 리허설이 통과해야만 적용한 다음 한 번 더 점검한다(v1.12).

