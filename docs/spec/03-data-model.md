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
| corporate_actions | action_id, symbol, action_type(CASH_DIVIDEND/CANCEL), record_date, pay_date, amount_per_share, cancels_id | v1.17에서 구현. 아래 "권리처리" 절 |

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
8. 명령은 계좌 하나를 골라 실행한다(`--account`, 기본은 `A0001`). 화면도 계좌 하나를 골라 본다: 계좌는 화면 정의의 조회조건이 아니라 플랫폼이 조회 트랜잭션마다 함께 전달하고, 서버는 형식과 등록 여부를 확인한다.

### 계좌 운영 (v1.13)

1. 고객과 계좌는 명령으로 등록한다(`customer add`, `account add`). 계좌를 등록하면 `OPEN` 상태 행이 함께 생긴다. Phase 4 전에는 모의투자(PAPER) 계좌만 등록한다.
2. 계좌의 인증정보는 `credential_ref`를 머리말로 하는 환경변수에서만 읽는다. `KIS_2`이면 `MINISTP_KIS_2_APP_KEY`, `MINISTP_KIS_2_APP_SECRET`, `MINISTP_KIS_2_ACCOUNT`, `MINISTP_KIS_2_HTS_ID`. 값이 없으면 실행할 때 물어보고 저장하지 않는다. 다른 계좌의 값으로 대신하지 않는다.
3. `credential_ref`는 계좌마다 달라야 한다(DB 유일 제약). 두 계좌의 환경변수가 같은 증권사 계좌번호를 가리키면 명령을 시작하지 않는다. 접근 토큰 캐시 파일도 계좌마다 따로 둔다(토큰은 앱키 하나에 속한다).
4. `open`, `run`, `order`, `daily`, `status`는 고른 계좌의 원장만 읽고 쓴다. 마감, 대사, 무오류 연속 영업일(R-74)도 계좌별이다.
5. 주문 ID(`oms_orders.client_id`)는 DB 전체에서 유일하다. 기본 계좌는 `S날짜-순번`, 다른 계좌는 `계좌ID-S날짜-순번`이다.
6. 원장을 쓰는 명령은 계좌와 상관없이 한 번에 하나만 실행된다(쓰기 잠금은 DB 하나에 하나). 여러 계좌의 `run`을 동시에 띄우는 것은 아직 지원하지 않는다.
7. 계좌 상태에 따른 주문 거부는 R-39를 따른다.

## 공통코드 (v1.14, ERD v2.1)

스키마 제약과 프로그램에 흩어져 있던 코드(`SUSPENDED`, `BREAK`, `FEE_DIFF`, `VALUATION_REVERSAL` 등)에 코드명을 붙여 한곳에서 관리한다.

| 테이블 | 주요 컬럼 | 비고 |
|---|---|---|
| code_groups | group_id, name, description, used_by(쓰는 테이블.컬럼), fixed | 코드 그룹. `fixed`는 프로그램이 정한 코드라는 뜻 |
| common_codes | group_id, code, name, description, sort_order, in_use, updated_at | 코드와 코드명. 키는 (group_id, code) |

1. **전용 테이블과 공통코드의 경계**: 원장이 직접 참조하는 코드는 전용 테이블에 두고 FK로 묶는다. 계정과목(`accounts_chart`)이 그렇다. 이름이 바뀌어도 아무것도 깨지지 않는 코드, 곧 이름표는 공통코드에 둔다.
2. **공통코드는 FK로 묶지 않는다**: 코드 컬럼이 공통코드를 FK로 참조하지 않는다. 허용 값은 지금처럼 컬럼의 CHECK 제약과 프로그램이 정한다. 대신 `code check`가 세 가지를 대조한다. (가) CHECK 목록이 있는 컬럼의 허용 값과 그 그룹의 코드가 같다. (나) 프로그램이 정의한 코드(이벤트 유형, 대사 원인, 주문 상태, 거부 사유 등)에 모두 행이 있다. (다) 그룹의 `used_by` 컬럼에 실제로 저장된 값에 모두 코드명이 있다.
3. **코드는 지우지 않는다**: 이미 저장된 행이 그 코드를 들고 있기 때문이다. 코드명과 설명을 바꾸거나 사용 여부를 끌 수만 있다. 코드와 그룹 ID의 변경, 삭제, 비우기는 DB 트리거가 막는다.
4. **고정 그룹과 운영자 그룹**: 시드로 들어가는 그룹(v2.1의 18개, v2.3부터 감사 로그 3개를 더해 21개)은 모두 고정(`fixed`)이다. 코드명만 바꿀 수 있고, 코드를 늘리거나 끄는 것은 프로그램·스키마 변경(마이그레이션)과 함께 한다. 운영자가 만든 그룹(`code group-add`)은 `code add`, `code use`로 코드를 늘리고 끌 수 있다.
5. **시드는 한 벌**: 새 DB는 `ministp.codes.SEED`에서, 기존 DB는 마이그레이션 `0002_common_codes`(v2.2의 코드 세 개는 `0003_day_close`)에서 같은 행을 받는다. 테스트가 두 결과를 비교한다.
6. **변경은 한 벌의 명령 함수로**: `code list [그룹]`, `code rename 그룹 코드 코드명`, `code group-add`, `code add`, `code use 그룹 코드 on|off`, `code check`. 화면(STP-4600 코드 관리)은 계정과목과 공통코드 그룹을 한 목록에서 보여 주고, 그리드 오른쪽 위 버튼(그룹 추가, 코드 추가, 코드명 변경, 사용 여부)이 같은 명령 함수를 부른다. 그래서 위 3·4번 규칙은 화면에서도 그대로다. 계정과목은 화면에서 바꾸지 않는다.

## 일 마감 (v1.15, ERD v2.2, 스펙 D6)

| 테이블·뷰 | 주요 컬럼 | 비고 |
|---|---|---|
| day_closes | account_id, date, event_seq, net_assets, units, nav_per_1000, closed_at | 마감 이벤트마다 한 행. 키는 (account_id, date). 그날의 확정 기준가 |
| last_close_v | account_id, date, event_seq, ... | 계좌별 마지막 마감 |
| abor_positions_v | account_id, closed_date, symbol, qty, book_cost | 마지막 마감 시점의 보유 (그 뒤 이벤트 제외) |
| unclosed_events_v | account_id, seq, event_type, recorded_at | 마지막 마감 뒤의 이벤트. IBOR와 ABOR의 차이 |

1. **마감은 이벤트다**: 원장 이벤트 `DayClosedEvent(date)`가 원천이고 `day_closes`는 그 투영이다(다른 파생 행처럼 재생 결과와 비교한다). 계좌마다 따로 마감한다.
2. **마감일 잠금은 DB에도 있다**: `journal_entries`, `cash_ledger`, `day_closes`에 넣는 행의 날짜가 같은 계좌의 마감일 이전이면 트리거 `ministp_forbid_closed_day`가 거부한다(R-78). 프로그램을 거치지 않은 입력도 마감한 날을 바꾸지 못한다. `position_ledger`의 매매일은 실제 매매일이라 잠그지 않는다(정정 이벤트가 마감한 날의 매매일을 가진다).
3. **이벤트 유형 두 개, 전표 유형 한 개 추가**: `DayClosedEvent`, `LateExecutionEvent`, `LATE_EXECUTION`. 공통코드에 코드명이 함께 들어간다(마이그레이션 `0003_day_close`, `common_codes`에 세 행 추가).
4. **마이그레이션 직후에는 아무것도 잠기지 않는다**: 마감한 날이 없기 때문이다. 적용 뒤 첫 `daily`가 그날을 마감하면서 그 이전 날짜가 모두 닫힌다.
5. **명령**: `daily`(깨끗한 날은 마감까지), `close [--date]`(불일치를 판단한 날을 직접 마감), `status`(어디까지 마감했는지와 그 뒤 이벤트 수).

## 감사 로그 (v1.16, ERD v2.3)

| 테이블 | 주요 컬럼 | 비고 |
|---|---|---|
| audit_log | id, at, channel, actor, action, outcome, account_id, target, detail, message, prev_hash, hash | 요청마다 한 행. 번호·시각·해시는 DB가 채움. 수정·삭제 불가 |

1. **원장과 다른 것을 기록한다**: `ledger_events`는 장부에 일어난 일이고 `audit_log`는 운영자가 시스템에 한 일이다. 거부된 요청은 원장에 아무 흔적이 없으므로 여기에만 남는다(R-85).
2. **코드 컬럼 세 개**: `channel`(CLI, UI, SYSTEM), `outcome`(DONE, REFUSED, FAILED), `action`(ORDER, DAILY, CLOSE 등 14개). 공통코드 그룹 `AUDIT_CHANNEL`, `AUDIT_OUTCOME`, `AUDIT_ACTION`에 코드명이 있다. `action`은 CHECK 목록 대신 프로그램과 공통코드 대조(`code check`)로 지킨다. 행위가 늘 때마다 테이블 제약을 바꾸지 않기 위해서다.
3. **계좌와 대상**: `account_id`는 계좌에 대한 일일 때만 채운다(등록된 계좌만 참조하므로, 없는 계좌에 대한 거부는 `detail.account`에 적는다). `target`은 주문번호, 대사 행 번호, `그룹.코드`, 날짜처럼 그 일이 가리키는 것이다. 거부된 주문은 주문번호가 없어 비운다.
4. **해시 사슬**: 트리거 `ministp_audit_chain`이 넣는 순간 번호, 시각, 앞 줄의 해시(`prev_hash`), 자기 해시(`hash`)를 채운다. 호출하는 쪽이 넣은 값은 덮어쓴다. 해시 계산은 DB 함수 `ministp_audit_hash` 하나뿐이라, 점검도 같은 함수로 다시 계산해 비교한다(R-87).
5. **명령**: `audit [--limit N] [--account 계좌ID] [--action 코드] [--outcome 코드]`는 최근 줄부터 보여주고 마지막 줄 번호와 해시를 알려준다. 읽기만 하고, 보는 일 자체는 남기지 않는다.
6. **마이그레이션**: `0004_audit_log`는 테이블, 트리거, 함수, 공통코드 그룹 세 개를 만든다. 기존 행은 건드리지 않는다. 적용 뒤 첫 줄은 그 적용(`DB_MIGRATE`)이다. 감사 로그 조회 화면은 다음 PR.

## 권리처리: 현금배당 (v1.17, ERD v2.4)

| 테이블·뷰 | 주요 컬럼 | 비고 |
|---|---|---|
| corporate_actions | action_id, symbol, action_type, record_date, pay_date, amount_per_share, cancels_id, note, registered_at | 권리 원장. 종목 단위, 모든 계좌가 함께 읽음. 수정·삭제 불가, 취소는 취소 행 |
| corporate_actions_v | action_id, symbol, ..., canceled_at, cancel_reason | 권리 목록과 취소 여부 |
| dividends_v | account_id, action_id, symbol, qty, accrued_gross, accrued_on, paid_gross, tax, paid_on, canceled_on, status | 계좌별 배당 현황. 원장 이벤트에서 바로 읽음 |

1. **권리는 기준정보, 배당은 원장 이벤트**: 권리 원장은 종목에 생긴 일(누구에게나 같은 사실)이고, 계좌가 받은 배당은 그 계좌의 원장 이벤트 `DividendAccrualEvent`(인식), `DividendPaymentEvent`(입금), `DividendCancelEvent`(인식 취소)다. 이벤트가 권리 번호·수량·금액을 담고 있어서 재생에는 권리 원장이 필요 없다.
2. **계좌별 프로젝션 테이블은 만들지 않았다**: 분개와 예수금 행은 기존 `journal_entries`·`journal_lines`·`cash_ledger`에 들어가고(전표 유형 `DIVIDEND_ACCRUAL`, `DIVIDEND`, `DIVIDEND_CANCEL`, 원천 참조 `CA-권리번호`), 현황은 뷰 `dividends_v`가 이벤트에서 계산한다.
3. **취소는 행을 더한다**: `action_type = 'CANCEL'`, `cancels_id`가 대상 권리를 가리킨다(권리 하나에 취소 하나, 유일 제약). 배당 행과 취소 행의 모양은 CHECK 제약이 지킨다.
4. **daily의 순서** (R-75의 ②와 ③ 사이): 권리 원장을 번호 순으로 읽어 ① 취소된 권리의 인식 되돌리기 ② 인식일이 된 권리의 인식(기준일 보유가 있을 때만) ③ 지급일이 된 권리의 입금(R-23 세금). 그 뒤에 평가와 기준가를 산출하므로 미수배당금이 그날 순자산에 들어간다(R-60).
5. **명령**: `dividend add 종목 --record 기준일 --pay 지급일 --per-share 주당배당금`, `dividend cancel 권리번호 사유`, `dividend list`, `dividend pay 권리번호 [--gross 세전] [--tax 세금] [--date 날짜] [--account 계좌]`(증권사가 실제로 준 금액이 예상과 다르거나 원장 시작 전 보유분일 때). 모두 감사 로그에 남는다.
6. **한계**: 인식한 뒤에 기준일 이전 매매일의 체결이 뒤늦게 들어오면(R-78) 권리 수량이 달라질 수 있다. 자동으로 고치지 않으므로 취소 후 다시 등록해 인식을 새로 한다. 증권사 권리 내역 조회로 자동 등록하는 것과 권리 화면은 다음 PR.

## 입출금·대체 (v1.18, ERD v2.5)

| 뷰 | 주요 컬럼 | 비고 |
|---|---|---|
| cash_movements_v | account_id, event_seq, date, kind, amount, transfer_id, counter_account | 계좌에 넣거나 뺀 돈. 들어오면 +, 나가면 -. 원장 이벤트에서 바로 읽음 |
| transfers_v | transfer_id, date, from_account, to_account, amount, out_seq, in_seq | 계좌 간 대체. 보낸 쪽과 받은 쪽을 한 행으로 |

1. **테이블은 늘지 않았다**: 출금은 `WithdrawalEvent`, 대체는 보낸 계좌의 `TransferOutEvent`와 받은 계좌의 `TransferInEvent`다. 분개와 예수금 행은 기존 `journal_entries`·`journal_lines`·`cash_ledger`에 들어가고(전표 유형과 예수금 사유 `WITHDRAWAL`, `TRANSFER_OUT`, `TRANSFER_IN`, 대체의 원천 참조는 `TR-번호`), 내역은 뷰가 이벤트에서 계산한다.
2. **대체는 이벤트 두 개, 트랜잭션 하나**: 이벤트는 계좌 하나에 속하므로(ERD v2.0) 대체는 계좌마다 하나씩 두 이벤트로 남는다. 둘은 같은 대체 번호를 담고 서로의 계좌를 가리킨다. 부분 유일 인덱스 `ledger_events_transfer_key`가 대체 번호 하나에 보낸 쪽 이벤트 하나, 받은 쪽 이벤트 하나만 허용한다.
3. **좌수는 이벤트에 담지 않는다**: 발행·환매 좌수는 그 계좌의 직전 기준가로 재생할 때 계산된다(R-61). `cash list`와 감사 로그의 `detail`(금액, 좌수, 적용 기준가, 메모)에서 볼 수 있다. 마감 행(`day_closes.units`)은 재생 결과와 대조된다.
4. **명령**: `cash list [--account 계좌]`(내역, 예수금, 출금가능금액, 좌수), `cash deposit 금액`, `cash withdraw 금액`, `cash transfer 금액 --to 받는계좌`(`--account`가 보내는 계좌). 셋 다 `[--date 날짜] [--memo 메모]`를 받고 감사 로그에 남는다(`CASH_DEPOSIT`, `CASH_WITHDRAW`, `CASH_TRANSFER`, 거부된 요청 포함). `status`에도 출금가능금액이 나온다.
5. **한계**: 화면은 다음 PR. 증권사 입출금 내역 조회로 자동 기록하지 않는다(리필이 기록하고 대사가 확인한다). 장부의 마지막 기록보다 앞선 날짜의 입출금은 그날로 넣지 못하고 열려 있는 날짜로 기록한다.

## 수동 조정 (v1.19, ERD v2.6)

| 뷰 | 주요 컬럼 | 비고 |
|---|---|---|
| adjustments_v | account_id, adjustment_id, event_seq, date, reason, amount, tax, cash_amount, memo, recon_result_id, canceled_on, cancel_reason | 조정과 그 취소를 한 행으로. 원장 이벤트에서 바로 읽음 |

1. **테이블은 늘지 않았다**: 조정은 `AdjustmentEvent`, 취소는 `AdjustmentCancelEvent`다. 분개와 예수금 행은 기존 테이블에 들어간다(전표 유형과 예수금 사유 `ADJUSTMENT`, `ADJUSTMENT_CANCEL`, 원천 참조 `ADJ-번호`). 취소 전표는 `reverses_entry_id`로 조정 전표를 가리킨다.
2. **계정과목 세 개 추가**: 이자수익(4040), 잡이익(4090), 잡손실(5090). `accounts_chart`에 행이 늘어난다(마이그레이션 `0007_adjustments`의 `changes-data`에 적음). 기존 계정은 그대로다.
3. **사유가 계정을 정한다**: 사유 코드와 상대 계정의 짝은 프로그램에 있고(`ledger.ADJUSTMENT_ACCOUNTS`), 공통코드 그룹 `ADJUSTMENT_REASON`이 코드명을 준다. 계정을 직접 고르는 입력은 없다.
4. **번호와 유일성**: 조정 번호는 DB 전체에서 하나의 순번이고, 부분 유일 인덱스 `ledger_events_adjustment_key`가 번호 하나에 조정 이벤트 하나, 취소 이벤트 하나만 허용한다. 취소는 조정이 있는 계좌의 원장에서만 할 수 있다.
5. **불일치 행과의 연결**: `recon_result_id`는 이 조정이 정리하는 대사 불일치 행이다(없어도 된다). FK는 걸지 않았고(이벤트 payload 안의 값이다), 무결성 점검이 그 행이 같은 계좌의 불일치인지 확인한다.
6. **명령**: `adjust list [--account 계좌]`, `adjust add 사유 금액 --memo 메모 [--tax 세금] [--break 불일치행] [--date 날짜]`, `adjust cancel 조정번호 사유 [--date 날짜]`. 모두 감사 로그에 남는다(`ADJUSTMENT_ADD`, `ADJUSTMENT_CANCEL`, 거부된 요청 포함).
7. **한계**: 화면은 다음 PR. 조정 금액에 상한은 두지 않았다(사유 목록, 메모, 감사 로그, 취소 전표로 통제한다). 증권사 거래내역 조회로 자동 기록하지 않는다.

## 원장 무결성 점검 (v1.11)

`python -m ministp verify`는 저장된 원장이 이벤트를 처음부터 다시 계산한 결과와 같은지 읽기 전용으로 확인한다. 쓰기 잠금을 잡지 않으므로 `run`이 떠 있어도 실행할 수 있다. 하나라도 어긋나면 종료 코드 1을 돌려준다.

| 점검 | 확인하는 것 |
|---|---|
| 이벤트 순번 | `ledger_events.seq`가 1부터 빈 번호 없이 이어진다 |
| 수정·삭제 방지 | append-only 테이블 16개의 수정·삭제·비우기 금지 트리거, 분개 대차 트리거, 마감일 잠금 트리거(v1.15), 감사 로그 사슬 트리거(v1.16)가 켜져 있다 (R-04, R-50, R-78, R-87) |
| 분개 대차 | 모든 분개의 차변 합 = 대변 합 |
| 감사 로그 (v1.16) | `audit_log`의 번호가 1부터 빈 번호 없이 이어지고, 각 줄의 해시가 앞 줄의 해시와 자기 내용으로 다시 계산한 값과 같다 (R-87) |
| 권리 (v1.17) | 계좌에 기록된 배당이 모두 권리 원장에 있고, 종목이 같고, 인식 금액 = 수량 × 주당 배당금이며, 취소된 권리가 입금으로 기록된 계좌가 없다 (R-59) |
| 대체 (v1.18) | 대체마다 보낸 쪽 이벤트와 받은 쪽 이벤트가 하나씩 있고, 서로의 계좌를 가리키며, 금액과 날짜가 같고, 두 계좌가 같은 고객의 것이다 (R-65) |
| 분개 재생 | 저장된 분개가 이벤트 재생 결과와 건별로 같다 |
| 계정 잔액 | 계정과목별 잔액이 재생 결과와 같다 |
| 보유 | `position_ledger`의 행과 종목별 수량·장부금액이 재생 결과와 같다 |
| 현금 | `cash_ledger`의 행과 예수금이 재생 결과와 같다 |
| 일 마감 (v1.15) | `day_closes`의 행이 마감 이벤트를 가리키고 확정 기준가가 재생 결과와 같다. `abor_positions_v`가 마감까지의 재생 결과와 같다 |
| 배당 (v1.17) | `dividends_v`가 재생 결과와 같고, 미수배당금 잔액이 입금 전 인식 금액의 합과 같다 (R-57) |
| 입출금 (v1.18) | `cash_movements_v`가 재생 결과와 같고, 투자원금 잔액이 입금·대체 입금·시작 보유의 합에서 출금·대체 출금을 뺀 값과 같다 (R-56) |
| 조정 (v1.19) | `adjustments_v`가 재생 결과와 같고, 조정이 가리키는 대사 불일치 행이 같은 계좌의 불일치다 (R-66) |

분개 재생부터 아래 여덟 가지(재생 비교)는 계좌마다 따로 확인한다(v1.12). 모든 명령이 시작할 때도 확인하며, 어긋나면 그 명령은 실행되지 않는다. 코드가 DB보다 앞서 있을 때는 `verify`도 시작하지 않는다. 대신 `db-migrate --dry-run`이 마이그레이션을 적용한 상태에서 같은 점검을 한 뒤 되돌리고, `--apply`는 그 리허설이 통과해야만 적용한 다음 한 번 더 점검한다(v1.12).

