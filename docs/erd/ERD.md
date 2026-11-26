# MiniSTP ERD

ERD 버전: v2.6 · 스키마 해시: `357e80163ddb`

이 문서는 `scripts/gen_erd.py`가 `src/ministp/store/schema.sql`로 만든 DB에서 읽어 생성해요. 직접 고치지 말고 스키마의 한글 주석(COMMENT)을 고친 뒤 다시 생성하세요. 모든 원장 테이블은 append-only(수정·삭제 불가)예요.

## 관계도

```mermaid
erDiagram
    accounts_chart {
        text code PK "계정코드"
        text name "계정명"
        char1 normal_side "정상잔액 방향 (D 차변 / C 대변)"
    }
    customers {
        text customer_id PK "고객 ID"
        text alias "고객 별칭"
        timestamptz created_at "등록 시각"
    }
    trading_accounts {
        text account_id PK "계좌 ID"
        text customer_id FK "고객 ID"
        text alias "계좌 별칭"
        text broker "증권사 (KIS)"
        text env "환경 (PAPER 모의 / LIVE 실전)"
        text account_type "계좌 유형 (GENERAL 위탁종합)"
        text credential_ref "인증정보 참조 (PC 환경변수 이름 머리말)"
        date opened_on "개설일"
        timestamptz created_at "등록 시각"
    }
    account_status_history {
        bigint id PK "행 번호"
        text account_id FK "계좌 ID"
        text status "상태 (OPEN 정상 / SUSPENDED 정지 / CLOSED 해지)"
        date effective_from "적용 시작일"
        text reason "사유"
        timestamptz created_at "기록 시각"
    }
    ledger_events {
        bigint seq PK "이벤트 순번"
        text event_type "이벤트 유형"
        jsonb payload "이벤트 내용"
        timestamptz recorded_at "기록 시각"
        text account_id FK "계좌 ID"
    }
    journal_entries {
        bigint entry_id PK "전표 번호"
        bigint event_seq FK "원장 이벤트 순번"
        date date "회계일"
        text kind "전표 유형"
        text source_id "원천 참조 (체결번호 등)"
        bigint reverses_entry_id FK "역분개 대상 전표 번호"
        text account_id FK "계좌 ID"
    }
    journal_lines {
        bigint line_id PK "라인 번호"
        bigint entry_id FK "전표 번호"
        text account_code FK "계정코드"
        bigint debit "차변 금액"
        bigint credit "대변 금액"
    }
    position_ledger {
        bigint id PK "행 번호"
        bigint event_seq FK "원장 이벤트 순번"
        date trade_date "매매일"
        text symbol "종목코드"
        text exec_id "체결 ID (계좌 안에서 유일, 기초 보유는 OPEN-종목코드)"
        bigint qty_delta "수량 증감"
        bigint cost_delta "장부금액 증감"
        bigint realized_pnl "실현손익"
        text account_id FK "계좌 ID"
    }
    cash_ledger {
        bigint id PK "행 번호"
        bigint event_seq FK "원장 이벤트 순번"
        date date "일자"
        text reason "사유 (입금, 결제 등)"
        bigint amount "금액 (+입금 / −출금)"
        text ref_id "참조 ID"
        text account_id FK "계좌 ID"
    }
    day_closes {
        text account_id PK,FK "계좌 ID"
        date date PK "마감일 (이 날짜까지 장부 마감)"
        bigint event_seq FK,UK "마감 이벤트 순번"
        bigint net_assets "마감 순자산"
        bigint units "마감 좌수"
        numeric(18,2) nav_per_1000 "마감 기준가 (1,000좌당)"
        timestamptz closed_at "마감 기록 시각"
    }
    audit_log {
        bigint id PK "행 번호 (1부터 빈 번호 없이)"
        timestamptz at "기록 시각"
        text channel "경로 (CLI 명령 / UI 화면 / SYSTEM 시스템)"
        text actor "실행한 PC 계정"
        text action "행위 코드"
        text outcome "결과 (DONE 처리 / REFUSED 거부 / FAILED 실패)"
        text account_id FK "계좌 ID (계좌와 무관한 행위는 비움)"
        text target "대상 (주문번호, 대사 행 번호, 코드 등)"
        jsonb detail "입력 내용 (인증키·계좌번호 제외)"
        text message "결과 메시지 (거부·실패 사유)"
        text prev_hash "앞 행의 해시"
        text hash UK "이 행의 해시 (앞 행 해시 + 이 행 내용)"
    }
    instruments {
        text symbol PK "종목코드"
        text name "종목명"
        text market "시장 (KOSPI / KOSDAQ)"
        text asset_type "자산 유형 (STOCK / ETF)"
    }
    corporate_actions {
        bigint action_id PK "권리 번호"
        text symbol FK "종목코드"
        text action_type "권리 유형 (CASH_DIVIDEND 현금배당 / CANCEL 취소)"
        date record_date "배당기준일"
        date pay_date "지급일"
        numeric(18,2) amount_per_share "주당 배당금 (세전, 원)"
        bigint cancels_id FK,UK "취소 대상 권리 번호 (취소 행일 때)"
        text note "메모 (취소 행은 취소 사유)"
        timestamptz registered_at "등록 시각"
    }
    oms_orders {
        text client_id PK "내부 주문번호"
        date trade_date "주문일"
        text symbol FK "종목코드"
        text side "매매구분 (BUY / SELL)"
        bigint qty "주문수량"
        bigint limit_price "지정가 (없으면 시장가)"
        timestamptz created_at "생성 시각"
        text account_id FK "계좌 ID"
    }
    oms_order_acks {
        text client_id PK,FK "내부 주문번호"
        text outcome "결과 (ACCEPTED 접수 / REJECTED 거부)"
        text order_no "증권사 주문번호 (영업일마다 다시 시작될 수 있음)"
        text org_no "주문조직번호 (취소 시 필요)"
        text detail "거부 사유"
        timestamptz created_at "기록 시각"
    }
    broker_snapshots {
        bigint snapshot_id PK "스냅샷 번호"
        date date "기준일"
        text kind "유형 (OPENING_BALANCE / DAILY_RECON / INTRADAY_RECON / FILL_NOTICE)"
        jsonb payload "응답 원본"
        timestamptz taken_at "수신 시각"
        text account_id FK "계좌 ID"
    }
    recon_results {
        bigint id PK "행 번호"
        bigint run_id FK "대사 실행 스냅샷 번호"
        date date "기준일"
        text item "대사 항목"
        text key "항목 키 (종목코드 등)"
        bigint internal_value "내부(원장) 값"
        bigint broker_value "증권사 값"
        bigint diff "차이 (절대값)"
        text status "상태 (MATCH 일치 / BREAK 불일치 / RESOLVED 해결)"
        text cause_code "원인 코드"
        text note "판단 메모"
        bigint supersedes_id FK "해결 대상 행 번호"
        timestamptz created_at "기록 시각"
        text account_id FK "계좌 ID"
    }
    code_groups {
        text group_id PK "코드 그룹 ID"
        text name "코드 그룹명"
        text description "코드 그룹 설명"
        text used_by "쓰는 곳 (이 코드를 저장하는 테이블.컬럼)"
        boolean fixed "코드 고정 여부 (프로그램이 정한 코드는 이름만 바꿀 수 있음)"
        timestamptz created_at "등록 시각"
    }
    common_codes {
        text group_id PK,FK "코드 그룹 ID"
        text code PK "코드"
        text name "코드명"
        text description "코드 설명"
        integer sort_order "표시 순서"
        boolean in_use "사용 여부"
        timestamptz updated_at "수정 시각"
    }
    schema_migrations {
        integer version PK "마이그레이션 번호"
        text name "마이그레이션 이름"
        text erd_version "적용 후 ERD 버전"
        text checksum "적용한 파일의 해시"
        timestamptz applied_at "적용 시각"
    }
    customers ||--o{ trading_accounts : "customer_id"
    trading_accounts ||--o{ account_status_history : "account_id"
    trading_accounts ||--o{ ledger_events : "account_id"
    ledger_events ||--o{ journal_entries : "event_seq, account_id"
    journal_entries ||--o{ journal_entries : "reverses_entry_id"
    journal_entries ||--o{ journal_lines : "entry_id"
    accounts_chart ||--o{ journal_lines : "account_code"
    ledger_events ||--o{ position_ledger : "event_seq, account_id"
    ledger_events ||--o{ cash_ledger : "event_seq, account_id"
    ledger_events ||--o{ day_closes : "event_seq, account_id"
    trading_accounts ||--o{ audit_log : "account_id"
    instruments ||--o{ corporate_actions : "symbol"
    corporate_actions ||--o{ corporate_actions : "cancels_id"
    instruments ||--o{ oms_orders : "symbol"
    trading_accounts ||--o{ oms_orders : "account_id"
    oms_orders ||--o{ oms_order_acks : "client_id"
    trading_accounts ||--o{ broker_snapshots : "account_id"
    broker_snapshots ||--o{ recon_results : "run_id, account_id"
    recon_results ||--o{ recon_results : "supersedes_id"
    code_groups ||--o{ common_codes : "group_id"
```

## 데이터 사전

### accounts_chart — 계정과목 (테이블)

| 컬럼 | 한글명 | 타입 | 필수 | 키 |
|---|---|---|---|---|
| `code` | 계정코드 | text | Y | PK |
| `name` | 계정명 | text | Y |  |
| `normal_side` | 정상잔액 방향 (D 차변 / C 대변) | character(1) | Y |  |

### customers — 고객 (별칭만 관리, 개인정보 없음) (테이블)

| 컬럼 | 한글명 | 타입 | 필수 | 키 |
|---|---|---|---|---|
| `customer_id` | 고객 ID | text | Y | PK |
| `alias` | 고객 별칭 | text | Y |  |
| `created_at` | 등록 시각 | timestamp with time zone | Y |  |

### trading_accounts — 위탁계좌 (계좌번호·인증키는 DB에 없음) (테이블)

| 컬럼 | 한글명 | 타입 | 필수 | 키 |
|---|---|---|---|---|
| `account_id` | 계좌 ID | text | Y | PK |
| `customer_id` | 고객 ID | text | Y | FK → customers.customer_id |
| `alias` | 계좌 별칭 | text | Y |  |
| `broker` | 증권사 (KIS) | text | Y |  |
| `env` | 환경 (PAPER 모의 / LIVE 실전) | text | Y |  |
| `account_type` | 계좌 유형 (GENERAL 위탁종합) | text | Y |  |
| `credential_ref` | 인증정보 참조 (PC 환경변수 이름 머리말) | text | Y |  |
| `opened_on` | 개설일 | date | Y |  |
| `created_at` | 등록 시각 | timestamp with time zone | Y |  |

### account_status_history — 계좌 상태 이력 (변경은 새 행으로 추가) (테이블)

| 컬럼 | 한글명 | 타입 | 필수 | 키 |
|---|---|---|---|---|
| `id` | 행 번호 | bigint | Y | PK |
| `account_id` | 계좌 ID | text | Y | FK → trading_accounts.account_id |
| `status` | 상태 (OPEN 정상 / SUSPENDED 정지 / CLOSED 해지) | text | Y |  |
| `effective_from` | 적용 시작일 | date | Y |  |
| `reason` | 사유 | text |  |  |
| `created_at` | 기록 시각 | timestamp with time zone | Y |  |

### ledger_events — 원장 이벤트 (모든 기록의 원천, 수정·삭제 불가) (테이블)

| 컬럼 | 한글명 | 타입 | 필수 | 키 |
|---|---|---|---|---|
| `seq` | 이벤트 순번 | bigint | Y | PK |
| `event_type` | 이벤트 유형 | text | Y |  |
| `payload` | 이벤트 내용 | jsonb | Y |  |
| `recorded_at` | 기록 시각 | timestamp with time zone | Y |  |
| `account_id` | 계좌 ID | text | Y | FK → trading_accounts.account_id |

### journal_entries — 분개 전표 (테이블)

| 컬럼 | 한글명 | 타입 | 필수 | 키 |
|---|---|---|---|---|
| `entry_id` | 전표 번호 | bigint | Y | PK |
| `event_seq` | 원장 이벤트 순번 | bigint | Y | FK → ledger_events.seq |
| `date` | 회계일 | date | Y |  |
| `kind` | 전표 유형 | text | Y |  |
| `source_id` | 원천 참조 (체결번호 등) | text | Y |  |
| `reverses_entry_id` | 역분개 대상 전표 번호 | bigint |  | FK → journal_entries.entry_id |
| `account_id` | 계좌 ID | text | Y | FK → ledger_events.account_id |

### journal_lines — 분개 명세 (차변·대변 라인) (테이블)

| 컬럼 | 한글명 | 타입 | 필수 | 키 |
|---|---|---|---|---|
| `line_id` | 라인 번호 | bigint | Y | PK |
| `entry_id` | 전표 번호 | bigint | Y | FK → journal_entries.entry_id |
| `account_code` | 계정코드 | text | Y | FK → accounts_chart.code |
| `debit` | 차변 금액 | bigint | Y |  |
| `credit` | 대변 금액 | bigint | Y |  |

### position_ledger — 보유수량 원장 (매매일 기준) (테이블)

| 컬럼 | 한글명 | 타입 | 필수 | 키 |
|---|---|---|---|---|
| `id` | 행 번호 | bigint | Y | PK |
| `event_seq` | 원장 이벤트 순번 | bigint | Y | FK → ledger_events.seq |
| `trade_date` | 매매일 | date | Y |  |
| `symbol` | 종목코드 | text | Y |  |
| `exec_id` | 체결 ID (계좌 안에서 유일, 기초 보유는 OPEN-종목코드) | text | Y |  |
| `qty_delta` | 수량 증감 | bigint | Y |  |
| `cost_delta` | 장부금액 증감 | bigint | Y |  |
| `realized_pnl` | 실현손익 | bigint |  |  |
| `account_id` | 계좌 ID | text | Y | FK → ledger_events.account_id |

### cash_ledger — 예수금 원장 (결제일 기준) (테이블)

| 컬럼 | 한글명 | 타입 | 필수 | 키 |
|---|---|---|---|---|
| `id` | 행 번호 | bigint | Y | PK |
| `event_seq` | 원장 이벤트 순번 | bigint | Y | FK → ledger_events.seq |
| `date` | 일자 | date | Y |  |
| `reason` | 사유 (입금, 결제 등) | text | Y |  |
| `amount` | 금액 (+입금 / −출금) | bigint | Y |  |
| `ref_id` | 참조 ID | text | Y |  |
| `account_id` | 계좌 ID | text | Y | FK → ledger_events.account_id |

### day_closes — 일 마감 (마감 이벤트마다 한 행, 그날의 확정 기준가) (테이블)

| 컬럼 | 한글명 | 타입 | 필수 | 키 |
|---|---|---|---|---|
| `account_id` | 계좌 ID | text | Y | PK, FK → ledger_events.account_id |
| `date` | 마감일 (이 날짜까지 장부 마감) | date | Y | PK |
| `event_seq` | 마감 이벤트 순번 | bigint | Y | UK, FK → ledger_events.seq |
| `net_assets` | 마감 순자산 | bigint | Y |  |
| `units` | 마감 좌수 | bigint | Y |  |
| `nav_per_1000` | 마감 기준가 (1,000좌당) | numeric(18,2) | Y |  |
| `closed_at` | 마감 기록 시각 | timestamp with time zone | Y |  |

### audit_log — 감사 로그 (누가 언제 무엇을 했고 어떻게 끝났는지, 수정·삭제 불가) (테이블)

| 컬럼 | 한글명 | 타입 | 필수 | 키 |
|---|---|---|---|---|
| `id` | 행 번호 (1부터 빈 번호 없이) | bigint | Y | PK |
| `at` | 기록 시각 | timestamp with time zone | Y |  |
| `channel` | 경로 (CLI 명령 / UI 화면 / SYSTEM 시스템) | text | Y |  |
| `actor` | 실행한 PC 계정 | text | Y |  |
| `action` | 행위 코드 | text | Y |  |
| `outcome` | 결과 (DONE 처리 / REFUSED 거부 / FAILED 실패) | text | Y |  |
| `account_id` | 계좌 ID (계좌와 무관한 행위는 비움) | text |  | FK → trading_accounts.account_id |
| `target` | 대상 (주문번호, 대사 행 번호, 코드 등) | text |  |  |
| `detail` | 입력 내용 (인증키·계좌번호 제외) | jsonb | Y |  |
| `message` | 결과 메시지 (거부·실패 사유) | text |  |  |
| `prev_hash` | 앞 행의 해시 | text | Y |  |
| `hash` | 이 행의 해시 (앞 행 해시 + 이 행 내용) | text | Y | UK |

### instruments — 종목 마스터 (테이블)

| 컬럼 | 한글명 | 타입 | 필수 | 키 |
|---|---|---|---|---|
| `symbol` | 종목코드 | text | Y | PK |
| `name` | 종목명 | text | Y |  |
| `market` | 시장 (KOSPI / KOSDAQ) | text | Y |  |
| `asset_type` | 자산 유형 (STOCK / ETF) | text | Y |  |

### corporate_actions — 권리 원장 (종목에 생긴 현금배당과 그 취소, 수정·삭제 불가) (테이블)

| 컬럼 | 한글명 | 타입 | 필수 | 키 |
|---|---|---|---|---|
| `action_id` | 권리 번호 | bigint | Y | PK |
| `symbol` | 종목코드 | text | Y | FK → instruments.symbol |
| `action_type` | 권리 유형 (CASH_DIVIDEND 현금배당 / CANCEL 취소) | text | Y |  |
| `record_date` | 배당기준일 | date |  |  |
| `pay_date` | 지급일 | date |  |  |
| `amount_per_share` | 주당 배당금 (세전, 원) | numeric(18,2) |  |  |
| `cancels_id` | 취소 대상 권리 번호 (취소 행일 때) | bigint |  | UK, FK → corporate_actions.action_id |
| `note` | 메모 (취소 행은 취소 사유) | text |  |  |
| `registered_at` | 등록 시각 | timestamp with time zone | Y |  |

### oms_orders — 주문 의도 (증권사 전송 전 기록) (테이블)

| 컬럼 | 한글명 | 타입 | 필수 | 키 |
|---|---|---|---|---|
| `client_id` | 내부 주문번호 | text | Y | PK |
| `trade_date` | 주문일 | date | Y |  |
| `symbol` | 종목코드 | text | Y | FK → instruments.symbol |
| `side` | 매매구분 (BUY / SELL) | text | Y |  |
| `qty` | 주문수량 | bigint | Y |  |
| `limit_price` | 지정가 (없으면 시장가) | bigint |  |  |
| `created_at` | 생성 시각 | timestamp with time zone | Y |  |
| `account_id` | 계좌 ID | text | Y | FK → trading_accounts.account_id |

### oms_order_acks — 주문 접수 결과 (증권사 응답) (테이블)

| 컬럼 | 한글명 | 타입 | 필수 | 키 |
|---|---|---|---|---|
| `client_id` | 내부 주문번호 | text | Y | PK, FK → oms_orders.client_id |
| `outcome` | 결과 (ACCEPTED 접수 / REJECTED 거부) | text | Y |  |
| `order_no` | 증권사 주문번호 (영업일마다 다시 시작될 수 있음) | text |  |  |
| `org_no` | 주문조직번호 (취소 시 필요) | text |  |  |
| `detail` | 거부 사유 | text |  |  |
| `created_at` | 기록 시각 | timestamp with time zone | Y |  |

### broker_snapshots — 증권사 응답 원본 (대사 실행, 체결통보, 원장 시작) (테이블)

| 컬럼 | 한글명 | 타입 | 필수 | 키 |
|---|---|---|---|---|
| `snapshot_id` | 스냅샷 번호 | bigint | Y | PK |
| `date` | 기준일 | date | Y |  |
| `kind` | 유형 (OPENING_BALANCE / DAILY_RECON / INTRADAY_RECON / FILL_NOTICE) | text | Y |  |
| `payload` | 응답 원본 | jsonb | Y |  |
| `taken_at` | 수신 시각 | timestamp with time zone | Y |  |
| `account_id` | 계좌 ID | text | Y | FK → trading_accounts.account_id |

### recon_results — 대사 결과 (판단은 새 행으로 추가) (테이블)

| 컬럼 | 한글명 | 타입 | 필수 | 키 |
|---|---|---|---|---|
| `id` | 행 번호 | bigint | Y | PK |
| `run_id` | 대사 실행 스냅샷 번호 | bigint | Y | FK → broker_snapshots.snapshot_id |
| `date` | 기준일 | date | Y |  |
| `item` | 대사 항목 | text | Y |  |
| `key` | 항목 키 (종목코드 등) | text | Y |  |
| `internal_value` | 내부(원장) 값 | bigint | Y |  |
| `broker_value` | 증권사 값 | bigint | Y |  |
| `diff` | 차이 (절대값) | bigint | Y |  |
| `status` | 상태 (MATCH 일치 / BREAK 불일치 / RESOLVED 해결) | text | Y |  |
| `cause_code` | 원인 코드 | text |  |  |
| `note` | 판단 메모 | text |  |  |
| `supersedes_id` | 해결 대상 행 번호 | bigint |  | FK → recon_results.id |
| `created_at` | 기록 시각 | timestamp with time zone | Y |  |
| `account_id` | 계좌 ID | text | Y | FK → broker_snapshots.account_id |

### code_groups — 공통코드 그룹 (테이블)

| 컬럼 | 한글명 | 타입 | 필수 | 키 |
|---|---|---|---|---|
| `group_id` | 코드 그룹 ID | text | Y | PK |
| `name` | 코드 그룹명 | text | Y |  |
| `description` | 코드 그룹 설명 | text |  |  |
| `used_by` | 쓰는 곳 (이 코드를 저장하는 테이블.컬럼) | text |  |  |
| `fixed` | 코드 고정 여부 (프로그램이 정한 코드는 이름만 바꿀 수 있음) | boolean | Y |  |
| `created_at` | 등록 시각 | timestamp with time zone | Y |  |

### common_codes — 공통코드 (코드와 코드명) (테이블)

| 컬럼 | 한글명 | 타입 | 필수 | 키 |
|---|---|---|---|---|
| `group_id` | 코드 그룹 ID | text | Y | PK, FK → code_groups.group_id |
| `code` | 코드 | text | Y | PK |
| `name` | 코드명 | text | Y |  |
| `description` | 코드 설명 | text |  |  |
| `sort_order` | 표시 순서 | integer | Y |  |
| `in_use` | 사용 여부 | boolean | Y |  |
| `updated_at` | 수정 시각 | timestamp with time zone | Y |  |

### schema_migrations — 스키마 마이그레이션 적용 기록 (테이블)

| 컬럼 | 한글명 | 타입 | 필수 | 키 |
|---|---|---|---|---|
| `version` | 마이그레이션 번호 | integer | Y | PK |
| `name` | 마이그레이션 이름 | text | Y |  |
| `erd_version` | 적용 후 ERD 버전 | text |  |  |
| `checksum` | 적용한 파일의 해시 | text | Y |  |
| `applied_at` | 적용 시각 | timestamp with time zone | Y |  |

### account_balances_v — 계좌별·계정별 잔액 (시산표) (뷰)

| 컬럼 | 한글명 | 타입 | 필수 | 키 |
|---|---|---|---|---|
| `account_id` | 계좌 ID | text |  |  |
| `code` | 계정코드 | text |  |  |
| `name` | 계정명 | text |  |  |
| `normal_side` | 정상잔액 방향 | character(1) |  |  |
| `balance` | 잔액 | numeric |  |  |

### positions_v — 계좌별·종목별 보유 현황 (뷰)

| 컬럼 | 한글명 | 타입 | 필수 | 키 |
|---|---|---|---|---|
| `account_id` | 계좌 ID | text |  |  |
| `symbol` | 종목코드 | text |  |  |
| `qty` | 보유수량 | numeric |  |  |
| `book_cost` | 장부금액 (매입금액) | numeric |  |  |

### cash_balance_v — 계좌별 예수금 잔액 (뷰)

| 컬럼 | 한글명 | 타입 | 필수 | 키 |
|---|---|---|---|---|
| `account_id` | 계좌 ID | text |  |  |
| `balance` | 잔액 | numeric |  |  |

### account_status_v — 계좌별 현재 상태 (뷰)

| 컬럼 | 한글명 | 타입 | 필수 | 키 |
|---|---|---|---|---|
| `account_id` | 계좌 ID | text |  |  |
| `customer_id` | 고객 ID | text |  |  |
| `alias` | 계좌 별칭 | text |  |  |
| `status` | 현재 상태 | text |  |  |
| `effective_from` | 상태 적용 시작일 | date |  |  |

### customer_positions_v — 고객별·종목별 보유 합계 (여러 계좌 합산) (뷰)

| 컬럼 | 한글명 | 타입 | 필수 | 키 |
|---|---|---|---|---|
| `customer_id` | 고객 ID | text |  |  |
| `symbol` | 종목코드 | text |  |  |
| `qty` | 보유수량 | numeric |  |  |
| `book_cost` | 장부금액 (매입금액) | numeric |  |  |

### last_close_v — 계좌별 마지막 마감 (뷰)

| 컬럼 | 한글명 | 타입 | 필수 | 키 |
|---|---|---|---|---|
| `account_id` | 계좌 ID | text |  |  |
| `date` | 마지막 마감일 | date |  |  |
| `event_seq` | 마감 이벤트 순번 | bigint |  |  |
| `net_assets` | 마감 순자산 | bigint |  |  |
| `units` | 마감 좌수 | bigint |  |  |
| `nav_per_1000` | 마감 기준가 (1,000좌당) | numeric(18,2) |  |  |
| `closed_at` | 마감 기록 시각 | timestamp with time zone |  |  |

### abor_positions_v — 마지막 마감 시점의 계좌별·종목별 보유 (ABOR) (뷰)

| 컬럼 | 한글명 | 타입 | 필수 | 키 |
|---|---|---|---|---|
| `account_id` | 계좌 ID | text |  |  |
| `closed_date` | 마감일 | date |  |  |
| `symbol` | 종목코드 | text |  |  |
| `qty` | 마감 보유수량 | numeric |  |  |
| `book_cost` | 마감 장부금액 (매입금액) | numeric |  |  |

### unclosed_events_v — 마지막 마감 뒤에 쌓인 이벤트 (IBOR와 ABOR의 차이) (뷰)

| 컬럼 | 한글명 | 타입 | 필수 | 키 |
|---|---|---|---|---|
| `account_id` | 계좌 ID | text |  |  |
| `seq` | 이벤트 순번 | bigint |  |  |
| `event_type` | 이벤트 유형 | text |  |  |
| `recorded_at` | 기록 시각 | timestamp with time zone |  |  |

### corporate_actions_v — 권리 목록 (취소 여부 포함) (뷰)

| 컬럼 | 한글명 | 타입 | 필수 | 키 |
|---|---|---|---|---|
| `action_id` | 권리 번호 | bigint |  |  |
| `symbol` | 종목코드 | text |  |  |
| `action_type` | 권리 유형 | text |  |  |
| `record_date` | 배당기준일 | date |  |  |
| `pay_date` | 지급일 | date |  |  |
| `amount_per_share` | 주당 배당금 (세전, 원) | numeric(18,2) |  |  |
| `note` | 메모 | text |  |  |
| `registered_at` | 등록 시각 | timestamp with time zone |  |  |
| `canceled_at` | 취소 시각 (취소되지 않았으면 비움) | timestamp with time zone |  |  |
| `cancel_reason` | 취소 사유 | text |  |  |

### dividends_v — 계좌별 배당 현황 (인식, 입금, 취소) (뷰)

| 컬럼 | 한글명 | 타입 | 필수 | 키 |
|---|---|---|---|---|
| `account_id` | 계좌 ID | text |  |  |
| `action_id` | 권리 번호 | bigint |  |  |
| `symbol` | 종목코드 | text |  |  |
| `qty` | 권리 수량 (기준일까지 결제된 보유) | bigint |  |  |
| `accrued_gross` | 인식한 배당금 (세전) | bigint |  |  |
| `accrued_on` | 인식일 | date |  |  |
| `paid_gross` | 입금된 배당금 (세전) | bigint |  |  |
| `tax` | 원천징수세액 | bigint |  |  |
| `paid_on` | 입금일 | date |  |  |
| `canceled_on` | 인식 취소일 | date |  |  |
| `status` | 상태 (ACCRUED 인식 / PAID 입금 / CANCELED 취소) | text |  |  |

### cash_movements_v — 입출금·대체 내역 (계좌에 넣거나 뺀 돈, 원장 이벤트에서 계산) (뷰)

| 컬럼 | 한글명 | 타입 | 필수 | 키 |
|---|---|---|---|---|
| `account_id` | 계좌 ID | text |  |  |
| `event_seq` | 원장 이벤트 순번 | bigint |  |  |
| `date` | 기록일 (돈이 움직인 날짜) | date |  |  |
| `kind` | 구분 (DEPOSIT 입금 / WITHDRAWAL 출금 / TRANSFER_IN 대체 입금 / TRANSFER_OUT 대체 출금) | text |  |  |
| `amount` | 금액 (들어오면 +, 나가면 -) | bigint |  |  |
| `transfer_id` | 대체 번호 (대체일 때) | bigint |  |  |
| `counter_account` | 상대 계좌 ID (대체일 때) | text |  |  |

### transfers_v — 계좌 간 대체 (보낸 쪽과 받은 쪽을 한 행으로) (뷰)

| 컬럼 | 한글명 | 타입 | 필수 | 키 |
|---|---|---|---|---|
| `transfer_id` | 대체 번호 | bigint |  |  |
| `date` | 대체일 | date |  |  |
| `from_account` | 보낸 계좌 ID | text |  |  |
| `to_account` | 받은 계좌 ID | text |  |  |
| `amount` | 금액 | bigint |  |  |
| `out_seq` | 보낸 쪽 원장 이벤트 순번 | bigint |  |  |
| `in_seq` | 받은 쪽 원장 이벤트 순번 | bigint |  |  |

### adjustments_v — 수동 조정 내역 (예탁금 이용료, 증권사와의 수수료·세금·배당 차이, 취소 여부 포함) (뷰)

| 컬럼 | 한글명 | 타입 | 필수 | 키 |
|---|---|---|---|---|
| `account_id` | 계좌 ID | text |  |  |
| `adjustment_id` | 조정 번호 | bigint |  |  |
| `event_seq` | 원장 이벤트 순번 | bigint |  |  |
| `date` | 기록일 | date |  |  |
| `reason` | 조정 사유 (INTEREST 예탁금 이용료 / FEE 수수료 차이 / TAX 거래세 차이 / DIVIDEND 배당금 차이 / WITHHOLDING 원천징수 차이 / OTHER 기타) | text |  |  |
| `amount` | 금액 (세전, 예수금이 늘면 +, 줄면 -) | bigint |  |  |
| `tax` | 원천징수세액 (예탁금 이용료에서 뗀 세금) | bigint |  |  |
| `cash_amount` | 예수금 변동액 (금액 - 세금) | bigint |  |  |
| `memo` | 메모 (조정하는 이유) | text |  |  |
| `recon_result_id` | 이 조정으로 정리하는 대사 불일치 행 ID | bigint |  |  |
| `canceled_on` | 취소일 (취소되지 않았으면 비움) | date |  |  |
| `cancel_reason` | 취소 사유 | text |  |  |

## 변경 이력

| 버전 | 일자 | 변경 내용 |
|---|---|---|
| v1.0 | 2026-11-21 | 최초 공개. 원장(이벤트·분개·보유·예수금), 계정과목, 종목, 주문 의도·접수, 증권사 응답 원본, 대사 결과 11개 테이블과 뷰 3개. 전 컬럼 한글 명칭. |
| v1.1 | 2026-11-21 | `oms_order_acks.order_no` 유일 제약 제거(증권사 주문번호는 영업일마다 다시 시작될 수 있음, 조회용 인덱스로 대체). 체결 ID를 `YYYYMMDD-주문번호-순번`으로 변경. |
| v2.0 | 2026-11-22 | 다계좌(스펙 D7). 고객 `customers`, 위탁계좌 `trading_accounts`, 계좌 상태 이력 `account_status_history` 신설. 원장·주문·대사 7개 테이블에 `account_id` 추가(파생 행은 이벤트와 같은 계좌만 참조하도록 복합 FK), 체결 ID 유일 범위를 계좌 안으로 변경. 뷰 3개를 계좌별로 바꾸고 `account_status_v`, `customer_positions_v` 추가. 마이그레이션 적용 기록 `schema_migrations` 포함. 기존 DB는 마이그레이션 `0001_multi_account`로 올림(기존 행 변경 없음). |
| v2.1 | 2026-11-22 | 공통코드. 코드 그룹 `code_groups`와 코드 `common_codes` 신설(마이그레이션 `0002_common_codes`). 스키마 제약과 프로그램에 흩어져 있던 코드 18개 그룹(계좌 상태, 대사 결과·항목·원인, 이벤트·전표 유형 등)에 코드명을 붙였다. 코드는 지우지 않고 이름을 바꾸거나 끄기만 한다(DB 트리거). 계정과목은 원장이 직접 참조하므로 전용 테이블 `accounts_chart`에 그대로 둔다. 기존 테이블과 기존 행은 바뀌지 않는다. |
| v2.2 | 2026-11-23 | 일 마감(스펙 D6, R-77~79). 마감 기록 `day_closes` 신설(마감 이벤트마다 한 행, 그날의 확정 순자산·좌수·기준가). 마감일과 그 이전 날짜로는 전표(`journal_entries`)·예수금 행(`cash_ledger`)·마감 행이 더 들어가지 않도록 DB 트리거 `ministp_forbid_closed_day` 추가. 뷰 `last_close_v`(계좌별 마지막 마감), `abor_positions_v`(마감 시점 보유), `unclosed_events_v`(마감 뒤 이벤트) 추가. 공통코드에 이벤트 유형 `LateExecutionEvent`·`DayClosedEvent`, 전표 유형 `LATE_EXECUTION` 추가. 기존 DB는 마이그레이션 `0003_day_close`로 올림(기존 행 변경 없음). |
| v2.3 | 2026-11-23 | 감사 로그(R-85~87). `audit_log` 신설(마이그레이션 `0004_audit_log`): 밖에서 시스템에 한 일(주문, 주문 취소, 일일 마감, 장부 마감, 불일치 판단, 종목·고객·계좌 등록, 계좌 상태와 공통코드 변경, 마이그레이션)마다 한 행, 처리된 것뿐 아니라 규칙이 거부한 요청과 실패한 요청도 남긴다. 번호·시각·해시는 DB 트리거 `ministp_audit_chain`이 채우고, 각 행은 앞 행의 해시를 이어 받아(`ministp_audit_hash`, SHA-256) 가운데 행을 고치거나 지우면 `verify`가 찾아낸다. 수정·삭제·비우기 금지 테이블 15개. 공통코드 그룹 3개(감사 로그 경로·결과·행위) 추가. 기존 데이터는 바뀌지 않는다. |
| v2.4 | 2026-11-23 | 권리처리: 현금배당(R-57~59). 권리 원장 `corporate_actions` 신설(마이그레이션 `0005_corporate_actions`): 종목별 배당기준일·지급일·주당 배당금을 한 번 등록하고, 잘못된 권리는 고치지 않고 취소 행으로 무효로 한다. 뷰 `corporate_actions_v`(취소 여부 포함), `dividends_v`(계좌별 인식·입금·취소 현황, 원장 이벤트에서 읽음) 추가. 배당 인식·입금·취소는 원장 이벤트로 남고 분개와 예수금 행은 기존 테이블에 들어간다. 수정·삭제·비우기 금지 테이블 16개. 공통코드 그룹 2개(권리 유형, 배당 상태)와 이벤트·전표·예수금 사유·감사 행위 코드 10개 추가. 기존 데이터는 바뀌지 않는다. |
| v2.5 | 2026-11-23 | 입출금·대체(R-56, R-61, R-64, R-65). 테이블은 그대로다. 뷰 `cash_movements_v`(계좌에 넣거나 뺀 돈: 입금, 출금, 대체 입금, 대체 출금), `transfers_v`(계좌 간 대체의 보낸 쪽과 받은 쪽을 한 행으로) 추가, 둘 다 원장 이벤트에서 읽는다. `ledger_events`에 부분 유일 인덱스 `ledger_events_transfer_key` 추가(대체 번호 하나에 보낸 쪽 이벤트 하나, 받은 쪽 이벤트 하나). 출금과 대체는 원장 이벤트로 남고 분개와 예수금 행은 기존 테이블에 들어간다. 공통코드에 이벤트·전표·예수금 사유·감사 행위 코드 12개 추가(마이그레이션 `0006_cash_movements`). 기존 데이터는 바뀌지 않는다. |
| v2.6 | 2026-11-23 | 수동 조정(R-66). 테이블은 그대로다. 뷰 `adjustments_v`(조정과 그 취소를 한 행으로: 사유, 금액, 세금, 예수금 변동액, 메모, 정리하는 대사 불일치 행, 취소일) 추가, 원장 이벤트에서 읽는다. `ledger_events`에 부분 유일 인덱스 `ledger_events_adjustment_key` 추가(조정 번호 하나에 조정 이벤트 하나, 취소 이벤트 하나). 계정과목 세 개 추가: 이자수익(4040), 잡이익(4090), 잡손실(5090). `accounts_chart.code`에 채번 규칙 제약 추가(첫째 자리 1~5인 숫자 4자리, 스펙 D4). 공통코드 그룹 1개(수동 조정 사유)와 이벤트·전표·예수금 사유·감사 행위·조정 사유 코드 14개 추가(마이그레이션 `0007_adjustments`). 기존 데이터는 바뀌지 않는다. |
