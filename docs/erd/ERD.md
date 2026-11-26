# MiniSTP ERD

ERD 버전: v2.14 · 스키마 해시: `4f11b05999b5`

이 문서는 `scripts/gen_erd.py`가 `src/ministp/store/schema.sql`로 만든 DB에서 읽어 생성해요. 직접 고치지 말고 스키마의 한글 주석(COMMENT)을 고친 뒤 다시 생성하세요. 모든 원장 테이블은 append-only(수정·삭제 불가)예요.

## 관계도

```mermaid
erDiagram
    accounts_chart {
        text code PK "계정코드"
        text name "계정명"
        char1 normal_side "정상잔액 방향 (D 차변 / C 대변)"
        boolean in_use "사용 여부 (사용 중지한 계정은 지난 전표만 가리키고 새 분개에 쓰지 않음)"
    }
    companies {
        text company_id PK "법인 ID"
        text alias "법인 별칭"
        timestamptz created_at "등록 시각"
    }
    trading_accounts {
        text account_id PK "계좌 ID"
        text company_id FK "법인 ID"
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
    company_erp_codes {
        bigint id PK "행 번호"
        text company_id FK "법인 ID"
        text erp_company_code "ERP 회사코드"
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
    master_loads {
        bigint load_id PK "적재 번호"
        date base_date UK "기준일 (파일을 받은 날)"
        timestamptz fetched_at "파일을 받은 시각"
        text kospi_sha256 "코스피 파일 해시 (SHA-256)"
        text kosdaq_sha256 "코스닥 파일 해시 (SHA-256)"
        integer rows_total "종목 수"
        integer rows_new "신규 (처음 나타난 종목 수)"
        integer rows_changed "변경 (속성이 바뀐 종목 수)"
        integer rows_delisted "빠짐 (파일에서 빠진 종목 수)"
        timestamptz loaded_at "적재 시각"
    }
    instrument_master {
        bigint master_id PK "이력 번호"
        text symbol "단축코드"
        date valid_from "적용 시작일 (이 속성이 처음 보인 기준일)"
        bigint load_id FK "적재 번호"
        boolean listed "상장 여부 (파일에서 빠지면 거짓인 행이 생김)"
        text isin "표준코드 (ISIN)"
        text name "종목명"
        text market "시장 (KOSPI / KOSDAQ)"
        text sec_group "증권그룹 구분 (ST 주권, EF ETF 등)"
        integer lot "매매수량단위"
        bigint par_value "액면가 (원)"
        date listed_on "상장일"
        bigint listed_shares_k "상장주수 (천 주)"
        text preferred "우선주 구분 (0 보통주)"
        boolean spac "스팩 여부"
        boolean halted "거래정지 여부"
        boolean liquidation "정리매매 여부"
        boolean managed "관리종목 여부"
        text warning "시장경고 구분 (00 없음, 01 투자주의, 02 투자경고, 03 투자위험)"
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
    app_users {
        text user_id PK "사용자 ID (지금은 PC 로그인 이름)"
        text name "이름"
        text created_by "등록한 사람"
        timestamptz created_at "등록 시각"
    }
    user_rights {
        bigint right_row_id PK "권한 행 번호"
        text user_id FK "사용자 ID"
        text account_id FK "계좌 ID (비어 있으면 모든 계좌)"
        text right_code "권한 (ORDER_WRITE 주문 작성 / ORDER_APPROVE 주문 승인 / RIGHTS_ADMIN 권한 관리)"
        boolean granted "부여 여부 (거짓이면 회수 행)"
        bigint max_order_value "1건 승인 한도 (원, 비어 있으면 한도 없음)"
        boolean self_approve "셀프 승인 허용 (자기가 쓴 요청을 승인할 수 있음)"
        date effective_from "적용 시작일"
        text granted_by "부여·회수한 사람"
        text reason "사유"
        timestamptz created_at "기록 시각"
    }
    order_requests {
        date trade_date PK "주문일 (요청 키, 이날 장중에만 나가고 장 마감에 만료)"
        integer request_no PK "요청 번호 (주문일마다 1부터, 계좌와 무관)"
        text account_id FK "계좌 ID"
        text order_type "주문 유형 (SINGLE 단건 / BASKET 바스켓)"
        text created_by FK "작성자"
        timestamptz created_at "작성 시각"
    }
    order_request_lines {
        date trade_date PK,FK "주문일"
        integer request_no PK,FK "요청 번호"
        integer line_no PK "줄 번호 (보내는 순서)"
        text symbol FK "종목코드"
        text side "매매 구분"
        bigint qty "수량"
        bigint limit_price "지정가 (비어 있으면 시장가)"
    }
    order_approvals {
        bigint id PK "이력 번호"
        date trade_date FK "주문일"
        integer request_no FK "요청 번호"
        text action "결재 행위 (REQUEST 요청 / APPROVE 승인 / REJECT 반려 / CANCEL 취소 / EXPIRE 만료)"
        text actor FK "처리한 사람 (만료는 비어 있음: run이 처리)"
        boolean self_approved "셀프 승인 (작성자가 승인, DB가 채움)"
        text reason "사유 (반려는 필수)"
        jsonb set_check "그때의 묶음 검증 결과 (R-98)"
        timestamptz at "처리 시각"
    }
    order_sends {
        bigint id PK "전송 이력 번호"
        date trade_date FK "주문일"
        integer request_no FK "요청 번호"
        integer line_no FK "줄 번호"
        text outcome "전송 결과 (SENT 전송 / FAILED 전송 실패)"
        text client_id FK,UK "주문 ID (전송한 주문)"
        text reason "사유 (전송 실패는 필수)"
        jsonb set_check "보내기 직전 묶음 검증 결과 (R-98)"
        timestamptz at "처리 시각"
    }
    erp_exports {
        bigint export_id PK "내보내기 번호"
        text account_id FK "계좌 ID (비어 있으면 모든 계좌)"
        date date_from "시작일"
        date date_to "종료일"
        text unit "전표 단위 (ENTRY 건별 / DAY 일별 합계)"
        text company_code "회사코드 (계좌 법인의 ERP 회사코드, 없으면 매핑 파일의 값, 법인이 여럿이면 쉼표로)"
        text document_type "전표유형 (매핑 파일, 없으면 빈 값)"
        text mapping_sha256 "매핑 파일 해시 (SHA-256, 비어 있으면 MiniSTP 계정 그대로)"
        integer documents "전표 수"
        integer lines "줄 수"
        bigint debit_total "차변 합계"
        bigint credit_total "대변 합계"
        text file_name "파일 이름"
        text file_sha256 "파일 해시 (SHA-256, 통제 파일에도 적음)"
        text created_by "내보낸 사람 (PC 로그인 이름)"
        timestamptz created_at "내보낸 시각"
    }
    erp_export_docs {
        bigint export_id PK,FK "내보내기 번호"
        text doc_no PK "증빙번호"
        text account_id FK "계좌 ID"
        date doc_date "전표일자"
    }
    erp_export_voids {
        bigint export_id PK,FK "내보내기 번호"
        text reason "사유"
        text voided_by "처리자 (PC 로그인 이름)"
        timestamptz voided_at "처리 시각"
    }
    close_steps {
        text step_code PK "단계 코드 (프로그램의 점검 로직과 짝)"
        text name "단계 이름"
        integer sort_order "순서"
        boolean in_use "사용 여부 (끄면 점검에서 빠짐)"
        boolean required "필수 여부 (선택 단계는 상태만 보이고 그날의 점검 완료를 막지 않음)"
        text screen "상세 화면"
        text asset_type "적용 자산 유형 (비어 있으면 모든 계좌, 있으면 그 자산을 거래·보유한 계좌에만)"
        text description "설명"
        timestamptz updated_at "마지막 변경 시각"
    }
    close_checks {
        bigint check_id PK "점검 번호"
        text account_id FK "계좌 ID"
        date check_date "영업일"
        text day_state "그날 상태 (DONE 점검 완료 / OPEN 진행 중 / ATTENTION 확인 필요)"
        integer done "끝난 필수 단계 수"
        integer steps "필수 단계 수"
        text command "점검한 명령 (daily, close, close-check)"
        text checked_by "점검한 사람 (PC 로그인 이름)"
        timestamptz checked_at "점검 시각"
    }
    close_check_steps {
        bigint check_id PK,FK "점검 번호"
        integer step_no PK "순서"
        text step_code FK "단계 코드"
        text name "단계 이름 (점검 때의 이름)"
        boolean required "필수 여부 (점검 때의 기준)"
        text state "상태 (DONE 완료 / NONE 대상 없음 / WAIT 대기 / WARN 주의 / BAD 확인 필요 / MISSING 누락)"
        text detail "내용 (그렇게 본 까닭)"
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
    companies ||--o{ trading_accounts : "company_id"
    trading_accounts ||--o{ account_status_history : "account_id"
    companies ||--o{ company_erp_codes : "company_id"
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
    master_loads ||--o{ instrument_master : "load_id"
    instruments ||--o{ oms_orders : "symbol"
    trading_accounts ||--o{ oms_orders : "account_id"
    oms_orders ||--o{ oms_order_acks : "client_id"
    trading_accounts ||--o{ broker_snapshots : "account_id"
    broker_snapshots ||--o{ recon_results : "run_id, account_id"
    recon_results ||--o{ recon_results : "supersedes_id"
    app_users ||--o{ user_rights : "user_id"
    trading_accounts ||--o{ user_rights : "account_id"
    trading_accounts ||--o{ order_requests : "account_id"
    app_users ||--o{ order_requests : "created_by"
    order_requests ||--o{ order_request_lines : "trade_date, request_no"
    instruments ||--o{ order_request_lines : "symbol"
    order_requests ||--o{ order_approvals : "trade_date, request_no"
    app_users ||--o{ order_approvals : "actor"
    order_request_lines ||--o{ order_sends : "trade_date, request_no, line_no"
    oms_orders ||--o{ order_sends : "client_id"
    trading_accounts ||--o{ erp_exports : "account_id"
    erp_exports ||--o{ erp_export_docs : "export_id"
    trading_accounts ||--o{ erp_export_docs : "account_id"
    erp_exports ||--o{ erp_export_voids : "export_id"
    trading_accounts ||--o{ close_checks : "account_id"
    close_checks ||--o{ close_check_steps : "check_id"
    close_steps ||--o{ close_check_steps : "step_code"
    code_groups ||--o{ common_codes : "group_id"
```

## 데이터 사전

### accounts_chart — 계정과목 (테이블)

| 컬럼 | 한글명 | 타입 | 필수 | 키 |
|---|---|---|---|---|
| `code` | 계정코드 | text | Y | PK |
| `name` | 계정명 | text | Y |  |
| `normal_side` | 정상잔액 방향 (D 차변 / C 대변) | character(1) | Y |  |
| `in_use` | 사용 여부 (사용 중지한 계정은 지난 전표만 가리키고 새 분개에 쓰지 않음) | boolean | Y |  |

### companies — 법인 (장부를 가진 회사, 별칭만 관리) (테이블)

| 컬럼 | 한글명 | 타입 | 필수 | 키 |
|---|---|---|---|---|
| `company_id` | 법인 ID | text | Y | PK |
| `alias` | 법인 별칭 | text | Y |  |
| `created_at` | 등록 시각 | timestamp with time zone | Y |  |

### trading_accounts — 위탁계좌 (계좌번호·인증키는 DB에 없음) (테이블)

| 컬럼 | 한글명 | 타입 | 필수 | 키 |
|---|---|---|---|---|
| `account_id` | 계좌 ID | text | Y | PK |
| `company_id` | 법인 ID | text | Y | FK → companies.company_id |
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

### company_erp_codes — 법인 ERP 회사코드 이력 (변경은 새 행으로 추가) (테이블)

| 컬럼 | 한글명 | 타입 | 필수 | 키 |
|---|---|---|---|---|
| `id` | 행 번호 | bigint | Y | PK |
| `company_id` | 법인 ID | text | Y | FK → companies.company_id |
| `erp_company_code` | ERP 회사코드 | text | Y |  |
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

### master_loads — 종목 마스터 적재 기록 (KIS 마스터 파일을 DB에 넣은 날마다 한 행, 수정·삭제 불가) (테이블)

| 컬럼 | 한글명 | 타입 | 필수 | 키 |
|---|---|---|---|---|
| `load_id` | 적재 번호 | bigint | Y | PK |
| `base_date` | 기준일 (파일을 받은 날) | date | Y | UK |
| `fetched_at` | 파일을 받은 시각 | timestamp with time zone | Y |  |
| `kospi_sha256` | 코스피 파일 해시 (SHA-256) | text | Y |  |
| `kosdaq_sha256` | 코스닥 파일 해시 (SHA-256) | text | Y |  |
| `rows_total` | 종목 수 | integer | Y |  |
| `rows_new` | 신규 (처음 나타난 종목 수) | integer | Y |  |
| `rows_changed` | 변경 (속성이 바뀐 종목 수) | integer | Y |  |
| `rows_delisted` | 빠짐 (파일에서 빠진 종목 수) | integer | Y |  |
| `loaded_at` | 적재 시각 | timestamp with time zone | Y |  |

### instrument_master — 종목 마스터 이력 (시장 전 종목, 속성이 바뀐 날에만 한 행, 수정·삭제 불가) (테이블)

| 컬럼 | 한글명 | 타입 | 필수 | 키 |
|---|---|---|---|---|
| `master_id` | 이력 번호 | bigint | Y | PK |
| `symbol` | 단축코드 | text | Y |  |
| `valid_from` | 적용 시작일 (이 속성이 처음 보인 기준일) | date | Y |  |
| `load_id` | 적재 번호 | bigint | Y | FK → master_loads.load_id |
| `listed` | 상장 여부 (파일에서 빠지면 거짓인 행이 생김) | boolean | Y |  |
| `isin` | 표준코드 (ISIN) | text | Y |  |
| `name` | 종목명 | text | Y |  |
| `market` | 시장 (KOSPI / KOSDAQ) | text | Y |  |
| `sec_group` | 증권그룹 구분 (ST 주권, EF ETF 등) | text | Y |  |
| `lot` | 매매수량단위 | integer | Y |  |
| `par_value` | 액면가 (원) | bigint | Y |  |
| `listed_on` | 상장일 | date |  |  |
| `listed_shares_k` | 상장주수 (천 주) | bigint | Y |  |
| `preferred` | 우선주 구분 (0 보통주) | text | Y |  |
| `spac` | 스팩 여부 | boolean | Y |  |
| `halted` | 거래정지 여부 | boolean | Y |  |
| `liquidation` | 정리매매 여부 | boolean | Y |  |
| `managed` | 관리종목 여부 | boolean | Y |  |
| `warning` | 시장경고 구분 (00 없음, 01 투자주의, 02 투자경고, 03 투자위험) | text | Y |  |

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

### app_users — 사용자 (주문을 쓰고 승인하는 사람, 추가만 가능) (테이블)

| 컬럼 | 한글명 | 타입 | 필수 | 키 |
|---|---|---|---|---|
| `user_id` | 사용자 ID (지금은 PC 로그인 이름) | text | Y | PK |
| `name` | 이름 | text | Y |  |
| `created_by` | 등록한 사람 | text | Y |  |
| `created_at` | 등록 시각 | timestamp with time zone | Y |  |

### user_rights — 결재 권한 이력 (부여·회수마다 한 행, 수정·삭제 불가) (테이블)

| 컬럼 | 한글명 | 타입 | 필수 | 키 |
|---|---|---|---|---|
| `right_row_id` | 권한 행 번호 | bigint | Y | PK |
| `user_id` | 사용자 ID | text | Y | FK → app_users.user_id |
| `account_id` | 계좌 ID (비어 있으면 모든 계좌) | text |  | FK → trading_accounts.account_id |
| `right_code` | 권한 (ORDER_WRITE 주문 작성 / ORDER_APPROVE 주문 승인 / RIGHTS_ADMIN 권한 관리) | text | Y |  |
| `granted` | 부여 여부 (거짓이면 회수 행) | boolean | Y |  |
| `max_order_value` | 1건 승인 한도 (원, 비어 있으면 한도 없음) | bigint |  |  |
| `self_approve` | 셀프 승인 허용 (자기가 쓴 요청을 승인할 수 있음) | boolean | Y |  |
| `effective_from` | 적용 시작일 | date | Y |  |
| `granted_by` | 부여·회수한 사람 | text | Y |  |
| `reason` | 사유 | text |  |  |
| `created_at` | 기록 시각 | timestamp with time zone | Y |  |

### order_requests — 주문 요청 (결재 한 건: 단건은 종목 한 줄, 바스켓은 여러 줄, 수정·삭제 불가) (테이블)

| 컬럼 | 한글명 | 타입 | 필수 | 키 |
|---|---|---|---|---|
| `trade_date` | 주문일 (요청 키, 이날 장중에만 나가고 장 마감에 만료) | date | Y | PK |
| `request_no` | 요청 번호 (주문일마다 1부터, 계좌와 무관) | integer | Y | PK |
| `account_id` | 계좌 ID | text | Y | FK → trading_accounts.account_id |
| `order_type` | 주문 유형 (SINGLE 단건 / BASKET 바스켓) | text | Y |  |
| `created_by` | 작성자 | text | Y | FK → app_users.user_id |
| `created_at` | 작성 시각 | timestamp with time zone | Y |  |

### order_request_lines — 주문 요청 줄 (요청의 종목 줄, 결재 요청 뒤에는 더하지 못함, 수정·삭제 불가) (테이블)

| 컬럼 | 한글명 | 타입 | 필수 | 키 |
|---|---|---|---|---|
| `trade_date` | 주문일 | date | Y | PK, FK → order_requests.trade_date |
| `request_no` | 요청 번호 | integer | Y | PK, FK → order_requests.request_no |
| `line_no` | 줄 번호 (보내는 순서) | integer | Y | PK |
| `symbol` | 종목코드 | text | Y | FK → instruments.symbol |
| `side` | 매매 구분 | text | Y |  |
| `qty` | 수량 | bigint | Y |  |
| `limit_price` | 지정가 (비어 있으면 시장가) | bigint |  |  |

### order_approvals — 결재 이력 (요청 단위: 요청·승인·반려·취소·만료마다 한 행, 수정·삭제 불가) (테이블)

| 컬럼 | 한글명 | 타입 | 필수 | 키 |
|---|---|---|---|---|
| `id` | 이력 번호 | bigint | Y | PK |
| `trade_date` | 주문일 | date | Y | FK → order_requests.trade_date |
| `request_no` | 요청 번호 | integer | Y | FK → order_requests.request_no |
| `action` | 결재 행위 (REQUEST 요청 / APPROVE 승인 / REJECT 반려 / CANCEL 취소 / EXPIRE 만료) | text | Y |  |
| `actor` | 처리한 사람 (만료는 비어 있음: run이 처리) | text |  | FK → app_users.user_id |
| `self_approved` | 셀프 승인 (작성자가 승인, DB가 채움) | boolean | Y |  |
| `reason` | 사유 (반려는 필수) | text |  |  |
| `set_check` | 그때의 묶음 검증 결과 (R-98) | jsonb | Y |  |
| `at` | 처리 시각 | timestamp with time zone | Y |  |

### order_sends — 전송 이력 (줄 단위: 승인된 요청의 줄마다 한 번, 수정·삭제 불가) (테이블)

| 컬럼 | 한글명 | 타입 | 필수 | 키 |
|---|---|---|---|---|
| `id` | 전송 이력 번호 | bigint | Y | PK |
| `trade_date` | 주문일 | date | Y | FK → order_request_lines.trade_date |
| `request_no` | 요청 번호 | integer | Y | FK → order_request_lines.request_no |
| `line_no` | 줄 번호 | integer | Y | FK → order_request_lines.line_no |
| `outcome` | 전송 결과 (SENT 전송 / FAILED 전송 실패) | text | Y |  |
| `client_id` | 주문 ID (전송한 주문) | text |  | UK, FK → oms_orders.client_id |
| `reason` | 사유 (전송 실패는 필수) | text |  |  |
| `set_check` | 보내기 직전 묶음 검증 결과 (R-98) | jsonb | Y |  |
| `at` | 처리 시각 | timestamp with time zone | Y |  |

### erp_exports — ERP 전송 이력 (erp export가 만든 파일마다 한 행, 수정·삭제 불가) (테이블)

| 컬럼 | 한글명 | 타입 | 필수 | 키 |
|---|---|---|---|---|
| `export_id` | 내보내기 번호 | bigint | Y | PK |
| `account_id` | 계좌 ID (비어 있으면 모든 계좌) | text |  | FK → trading_accounts.account_id |
| `date_from` | 시작일 | date | Y |  |
| `date_to` | 종료일 | date | Y |  |
| `unit` | 전표 단위 (ENTRY 건별 / DAY 일별 합계) | text | Y |  |
| `company_code` | 회사코드 (계좌 법인의 ERP 회사코드, 없으면 매핑 파일의 값, 법인이 여럿이면 쉼표로) | text | Y |  |
| `document_type` | 전표유형 (매핑 파일, 없으면 빈 값) | text | Y |  |
| `mapping_sha256` | 매핑 파일 해시 (SHA-256, 비어 있으면 MiniSTP 계정 그대로) | text |  |  |
| `documents` | 전표 수 | integer | Y |  |
| `lines` | 줄 수 | integer | Y |  |
| `debit_total` | 차변 합계 | bigint | Y |  |
| `credit_total` | 대변 합계 | bigint | Y |  |
| `file_name` | 파일 이름 | text | Y |  |
| `file_sha256` | 파일 해시 (SHA-256, 통제 파일에도 적음) | text | Y |  |
| `created_by` | 내보낸 사람 (PC 로그인 이름) | text | Y |  |
| `created_at` | 내보낸 시각 | timestamp with time zone | Y |  |

### erp_export_docs — ERP 전송 전표 (내보내기에 든 증빙번호마다 한 행, 수정·삭제 불가) (테이블)

| 컬럼 | 한글명 | 타입 | 필수 | 키 |
|---|---|---|---|---|
| `export_id` | 내보내기 번호 | bigint | Y | PK, FK → erp_exports.export_id |
| `doc_no` | 증빙번호 | text | Y | PK |
| `account_id` | 계좌 ID | text | Y | FK → trading_accounts.account_id |
| `doc_date` | 전표일자 | date | Y |  |

### erp_export_voids — ERP 전송 무효 (ERP가 받지 않은 내보내기, 그 전표는 다시 낼 수 있음, 수정·삭제 불가) (테이블)

| 컬럼 | 한글명 | 타입 | 필수 | 키 |
|---|---|---|---|---|
| `export_id` | 내보내기 번호 | bigint | Y | PK, FK → erp_exports.export_id |
| `reason` | 사유 | text | Y |  |
| `voided_by` | 처리자 (PC 로그인 이름) | text | Y |  |
| `voided_at` | 처리 시각 | timestamp with time zone | Y |  |

### close_steps — 마감 점검 단계 기준 (단계마다 한 행, 이름·순서·사용·필수·화면·자산 유형을 고침, 삭제 불가) (테이블)

| 컬럼 | 한글명 | 타입 | 필수 | 키 |
|---|---|---|---|---|
| `step_code` | 단계 코드 (프로그램의 점검 로직과 짝) | text | Y | PK |
| `name` | 단계 이름 | text | Y |  |
| `sort_order` | 순서 | integer | Y |  |
| `in_use` | 사용 여부 (끄면 점검에서 빠짐) | boolean | Y |  |
| `required` | 필수 여부 (선택 단계는 상태만 보이고 그날의 점검 완료를 막지 않음) | boolean | Y |  |
| `screen` | 상세 화면 | text |  |  |
| `asset_type` | 적용 자산 유형 (비어 있으면 모든 계좌, 있으면 그 자산을 거래·보유한 계좌에만) | text |  |  |
| `description` | 설명 | text | Y |  |
| `updated_at` | 마지막 변경 시각 | timestamp with time zone | Y |  |

### close_checks — 마감 점검 결과 (점검할 때마다 계좌·영업일마다 한 행, 수정·삭제 불가) (테이블)

| 컬럼 | 한글명 | 타입 | 필수 | 키 |
|---|---|---|---|---|
| `check_id` | 점검 번호 | bigint | Y | PK |
| `account_id` | 계좌 ID | text | Y | FK → trading_accounts.account_id |
| `check_date` | 영업일 | date | Y |  |
| `day_state` | 그날 상태 (DONE 점검 완료 / OPEN 진행 중 / ATTENTION 확인 필요) | text | Y |  |
| `done` | 끝난 필수 단계 수 | integer | Y |  |
| `steps` | 필수 단계 수 | integer | Y |  |
| `command` | 점검한 명령 (daily, close, close-check) | text | Y |  |
| `checked_by` | 점검한 사람 (PC 로그인 이름) | text | Y |  |
| `checked_at` | 점검 시각 | timestamp with time zone | Y |  |

### close_check_steps — 마감 점검 단계 결과 (점검 한 번의 단계마다 한 행, 수정·삭제 불가) (테이블)

| 컬럼 | 한글명 | 타입 | 필수 | 키 |
|---|---|---|---|---|
| `check_id` | 점검 번호 | bigint | Y | PK, FK → close_checks.check_id |
| `step_no` | 순서 | integer | Y | PK |
| `step_code` | 단계 코드 | text | Y | FK → close_steps.step_code |
| `name` | 단계 이름 (점검 때의 이름) | text | Y |  |
| `required` | 필수 여부 (점검 때의 기준) | boolean | Y |  |
| `state` | 상태 (DONE 완료 / NONE 대상 없음 / WAIT 대기 / WARN 주의 / BAD 확인 필요 / MISSING 누락) | text | Y |  |
| `detail` | 내용 (그렇게 본 까닭) | text | Y |  |

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
| `company_id` | 법인 ID | text |  |  |
| `alias` | 계좌 별칭 | text |  |  |
| `status` | 현재 상태 | text |  |  |
| `effective_from` | 상태 적용 시작일 | date |  |  |

### company_positions_v — 법인별·종목별 보유 합계 (여러 계좌 합산) (뷰)

| 컬럼 | 한글명 | 타입 | 필수 | 키 |
|---|---|---|---|---|
| `company_id` | 법인 ID | text |  |  |
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

### instrument_master_current_v — 종목 마스터 현재 (종목마다 가장 최근 이력 행) (뷰)

| 컬럼 | 한글명 | 타입 | 필수 | 키 |
|---|---|---|---|---|
| `symbol` | 단축코드 | text |  |  |
| `listed` | 상장 여부 | boolean |  |  |
| `name` | 종목명 | text |  |  |
| `market` | 시장 | text |  |  |
| `sec_group` | 증권그룹 구분 | text |  |  |
| `isin` | 표준코드 (ISIN) | text |  |  |
| `lot` | 매매수량단위 | integer |  |  |
| `par_value` | 액면가 (원) | bigint |  |  |
| `listed_on` | 상장일 | date |  |  |
| `listed_shares_k` | 상장주수 (천 주) | bigint |  |  |
| `preferred` | 우선주 구분 | text |  |  |
| `spac` | 스팩 여부 | boolean |  |  |
| `halted` | 거래정지 여부 | boolean |  |  |
| `liquidation` | 정리매매 여부 | boolean |  |  |
| `managed` | 관리종목 여부 | boolean |  |  |
| `warning` | 시장경고 구분 | text |  |  |
| `valid_from` | 적용 시작일 | date |  |  |
| `load_id` | 적재 번호 | bigint |  |  |

### instrument_master_match_v — 등록 종목과 종목 마스터 대조 (원장이 쓰는 종목마다 한 행) (뷰)

| 컬럼 | 한글명 | 타입 | 필수 | 키 |
|---|---|---|---|---|
| `symbol` | 종목코드 | text |  |  |
| `name` | 등록 종목명 | text |  |  |
| `market` | 등록 시장 | text |  |  |
| `asset_type` | 등록 자산 유형 | text |  |  |
| `master_name` | 마스터 종목명 | text |  |  |
| `master_market` | 마스터 시장 | text |  |  |
| `master_group` | 마스터 증권그룹 구분 | text |  |  |
| `listed` | 마스터 상장 여부 | boolean |  |  |
| `halted` | 거래정지 여부 | boolean |  |  |
| `liquidation` | 정리매매 여부 | boolean |  |  |
| `managed` | 관리종목 여부 | boolean |  |  |
| `warning` | 시장경고 구분 | text |  |  |
| `valid_from` | 마스터 적용 시작일 | date |  |  |
| `status` | 대조 결과 (MATCH 일치 / RENAMED 이름만 다름 / DIFFERENT 시장·구분 다름 / DELISTED 상장폐지 / MISSING 마스터에 없음) | text |  |  |

### user_rights_v — 현재 결재 권한 (사용자·계좌·권한마다 오늘 적용되는 최근 행, 부여된 것만) (뷰)

| 컬럼 | 한글명 | 타입 | 필수 | 키 |
|---|---|---|---|---|
| `user_id` | 사용자 ID | text |  |  |
| `user_name` | 이름 | text |  |  |
| `account_id` | 계좌 ID (비어 있으면 모든 계좌) | text |  |  |
| `right_code` | 권한 | text |  |  |
| `max_order_value` | 1건 승인 한도 (원) | bigint |  |  |
| `self_approve` | 셀프 승인 허용 | boolean |  |  |
| `effective_from` | 적용 시작일 | date |  |  |
| `granted_by` | 부여한 사람 | text |  |  |
| `reason` | 사유 | text |  |  |
| `right_row_id` | 권한 행 번호 | bigint |  |  |

### order_requests_v — 주문 요청 현황 (요청마다 한 행, 상태는 결재 이력의 마지막 행과 줄별 전송 결과) (뷰)

| 컬럼 | 한글명 | 타입 | 필수 | 키 |
|---|---|---|---|---|
| `trade_date` | 주문일 | date |  |  |
| `request_no` | 요청 번호 | integer |  |  |
| `account_id` | 계좌 ID | text |  |  |
| `order_type` | 주문 유형 | text |  |  |
| `created_by` | 작성자 | text |  |  |
| `created_at` | 작성 시각 | timestamp with time zone |  |  |
| `line_count` | 줄 수 | bigint |  |  |
| `amount` | 주문금액 합계 (지정가 줄만, 시장가 줄은 빠짐) | numeric |  |  |
| `status` | 상태 (PENDING 결재 대기 / APPROVED 승인·전송 대기 / REJECTED 반려 / CANCELED 취소 / SENT 전송 / SEND_FAILED 전송 실패 / SEND_UNKNOWN 결과 미확인 / EXPIRED 만료) | text |  |  |
| `approved_by` | 승인자 | text |  |  |
| `self_approved` | 셀프 승인 | boolean |  |  |
| `approved_at` | 승인 시각 | timestamp with time zone |  |  |
| `sent_lines` | 전송한 줄 수 | bigint |  |  |
| `failed_lines` | 전송 실패한 줄 수 | bigint |  |  |
| `last_reason` | 마지막 사유 | text |  |  |
| `status_at` | 상태 시각 | timestamp with time zone |  |  |
| `accepted_lines` | 증권사가 접수한 줄 수 | bigint |  |  |
| `rejected_lines` | 증권사가 거부한 줄 수 | bigint |  |  |
| `unknown_lines` | 보냈지만 접수 결과를 모르는 줄 수 (R-103) | bigint |  |  |

### order_request_lines_v — 주문 요청 줄 현황 (줄마다 한 행, 전송 결과와 주문 ID) (뷰)

| 컬럼 | 한글명 | 타입 | 필수 | 키 |
|---|---|---|---|---|
| `trade_date` | 주문일 | date |  |  |
| `request_no` | 요청 번호 | integer |  |  |
| `line_no` | 줄 번호 | integer |  |  |
| `account_id` | 계좌 ID | text |  |  |
| `order_type` | 주문 유형 | text |  |  |
| `symbol` | 종목코드 | text |  |  |
| `side` | 매매 구분 | text |  |  |
| `qty` | 수량 | bigint |  |  |
| `limit_price` | 지정가 | bigint |  |  |
| `amount` | 주문금액 (지정가 줄만) | bigint |  |  |
| `send_outcome` | 전송 결과 | text |  |  |
| `client_id` | 주문 ID | text |  |  |
| `send_reason` | 전송 실패 사유 | text |  |  |
| `sent_at` | 전송 시각 | timestamp with time zone |  |  |
| `ack_outcome` | 증권사 접수 결과 (ACCEPTED 접수 / REJECTED 거부, 비어 있으면 아직 모름) | text |  |  |
| `order_no` | 증권사 주문번호 | text |  |  |
| `ack_detail` | 접수 결과 내용 (거부 사유, 조회로 찾음 등) | text |  |  |
| `awaiting_ack` | 보냈지만 접수 결과를 모름 (R-103) | boolean |  |  |

### erp_exports_v — ERP 전송 현황 (내보내기마다 한 행, 무효 여부 포함) (뷰)

| 컬럼 | 한글명 | 타입 | 필수 | 키 |
|---|---|---|---|---|
| `export_id` | 내보내기 번호 | bigint |  |  |
| `account_id` | 계좌 ID (비어 있으면 모든 계좌) | text |  |  |
| `date_from` | 시작일 | date |  |  |
| `date_to` | 종료일 | date |  |  |
| `unit` | 전표 단위 (ENTRY 건별 / DAY 일별 합계) | text |  |  |
| `company_code` | 회사코드 | text |  |  |
| `document_type` | 전표유형 | text |  |  |
| `mapping_sha256` | 매핑 파일 해시 | text |  |  |
| `documents` | 전표 수 | integer |  |  |
| `lines` | 줄 수 | integer |  |  |
| `debit_total` | 차변 합계 | bigint |  |  |
| `credit_total` | 대변 합계 | bigint |  |  |
| `file_name` | 파일 이름 | text |  |  |
| `file_sha256` | 파일 해시 | text |  |  |
| `created_by` | 내보낸 사람 | text |  |  |
| `created_at` | 내보낸 시각 | timestamp with time zone |  |  |
| `status` | 상태 (EXPORTED 내보냄 / VOIDED 무효) | text |  |  |
| `void_reason` | 무효 사유 | text |  |  |
| `voided_by` | 무효 처리자 | text |  |  |
| `voided_at` | 무효 처리 시각 | timestamp with time zone |  |  |

### close_checks_last_v — 계좌·영업일마다 마지막 마감 점검 결과 (뷰)

| 컬럼 | 한글명 | 타입 | 필수 | 키 |
|---|---|---|---|---|
| `account_id` | 계좌 ID | text |  |  |
| `check_date` | 영업일 | date |  |  |
| `check_id` | 점검 번호 | bigint |  |  |
| `day_state` | 그날 상태 (DONE 점검 완료 / OPEN 진행 중 / ATTENTION 확인 필요) | text |  |  |
| `done` | 끝난 필수 단계 수 | integer |  |  |
| `steps` | 필수 단계 수 | integer |  |  |
| `command` | 점검한 명령 | text |  |  |
| `checked_by` | 점검한 사람 | text |  |  |
| `checked_at` | 점검 시각 | timestamp with time zone |  |  |

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
| v2.7 | 2026-11-24 | 종목 마스터 이력(R-94, R-95). 적재 기록 `master_loads`(KIS 마스터 파일을 넣은 기준일마다 한 행, 두 파일의 SHA-256과 신규·변경·빠짐 건수)와 종목 마스터 이력 `instrument_master`(시장 전 종목, 속성이 바뀐 날에만 한 행: 종목명, 시장, 증권그룹, 매매수량단위, 액면가, 상장주수, 거래정지·정리매매·관리종목·시장경고) 신설, 둘 다 수정·삭제·비우기 금지(테이블 18개). 파일에서 빠진 종목은 상장 여부가 거짓인 행으로 남는다. 가격(기준가, 시가총액, 거래량)은 담지 않는다. 등록 종목 `instruments`와는 FK로 묶지 않고 뷰 `instrument_master_current_v`(종목마다 최근 행), `instrument_master_match_v`(등록 종목과 마스터 대조) 추가. 공통코드 그룹 2개(증권그룹 구분, 종목 마스터 대조 결과)와 감사 행위 `MASTER_LOAD` 추가(마이그레이션 `0008_instrument_master`). 기존 데이터는 바뀌지 않는다. |
| v2.8 | 2026-11-24 | (초안, 운영 적용 전에 v2.9로 바뀜) 주문 결재(스펙 D9, R-99~101). 사용자 `app_users`(지금은 PC 로그인 이름이 ID), 결재 권한 이력 `user_rights`(주문 작성·주문 승인·권한 관리, 계좌별 또는 모든 계좌, 1건 승인 한도, 셀프 승인 허용, 적용 시작일, 부여·회수마다 한 행), 주문 요청 `order_requests`(주문일, 묶음 번호와 줄 번호, 종목·매매·수량·지정가, 작성자), 결재 이력 `order_approvals`(요청·승인·반려·취소·전송·전송 실패·만료마다 한 행, 그때의 묶음 검증 결과, 전송한 주문 ID) 신설, 넷 다 수정·삭제·비우기 금지(테이블 22개). 결재 순서는 DB 트리거 `ministp_order_approval_flow`가 지킨다(요청 다음에만 승인·반려, 승인 다음에만 전송, 요청과 취소는 작성자만, 셀프 승인 표시는 DB가 채움, 같은 요청은 잠그고 처리). 주문 ID 하나에 전송 기록 하나(부분 유일 인덱스). 뷰 `user_rights_v`(오늘 적용되는 권한), `order_requests_v`(요청마다 상태) 추가. 공통코드 그룹 3개(결재 권한, 결재 행위, 주문 요청 상태)와 감사 행위 7개 추가(마이그레이션 `0009_order_approval`). 기존 데이터는 바뀌지 않는다. |
| v2.9 | 2026-11-24 | 주문 결재 요청 번호 체계(11/24 사용자 피드백, v2.8 초안을 적용 전에 고침). 요청 키를 `Q날짜-순번` 글자에서 주문일 + 요청 번호(주문일마다 1부터, 계좌와 무관)로 바꾸고, 주문 유형 `order_type`(단건 SINGLE / 바스켓 BASKET)을 따로 둔다. 바스켓은 종목마다 요청이 아니라 요청 한 건이다: 종목 줄은 새 테이블 `order_request_lines`(요청 뒤에는 줄을 더하지 못함, 트리거 `ministp_order_lines_fixed`), 묶음 번호 `batch_id`는 없앤다. 결재 이력 `order_approvals`는 요청 단위(요청·승인·반려·취소·만료), 전송은 새 테이블 `order_sends`에 줄마다 한 번(전송·전송 실패, 주문 ID 유일, 승인 다음에만: 트리거 `ministp_order_send_flow`). 한 줄이라도 나간 요청은 취소·만료하지 않는다. 뷰 `order_requests_v`(요청마다 상태, 줄 수, 금액 합계, 전송·실패 줄 수)를 바꾸고 `order_request_lines_v`(줄마다 전송 결과) 추가. 수정·삭제·비우기 금지 테이블 24개. 공통코드 그룹 2개(주문 유형, 전송 결과) 추가, 결재 행위에서 전송·전송 실패를 뺀다. 마이그레이션은 `0009_order_approval` 하나(v2.7 → v2.9). |
| v2.10 | 2026-11-24 | 주문 결과 미확인 처리(R-103). 테이블은 그대로다. 뷰 `order_requests_v`가 증권사 접수 결과(`oms_order_acks`)를 함께 읽어서 상태에 결과 미확인 `SEND_UNKNOWN`(보낸 줄 가운데 답이 없는 줄이 있음)을 더하고, 전송한 줄을 다시 증권사가 접수·거부한 줄로 나누며(접수·거부·미확인 줄 수 컬럼 추가), 모든 줄이 거부되면 전송 실패로 본다. 뷰 `order_request_lines_v`에 접수 결과·증권사 주문번호·내용·결과 대기 여부 컬럼 추가. 공통코드에 요청 상태 `SEND_UNKNOWN`과 감사 행위 `ORDER_CONFIRM`(사람이 답 없던 주문을 확인) 추가(마이그레이션 `0010_send_unknown`). 기존 데이터는 바뀌지 않는다. |
| v2.11 | 2026-11-25 | ERP 전송 이력(R-104, 초안). ERP 전송 이력 `erp_exports`(`erp export`가 만든 파일마다 한 행: 계좌, 기간, 전표 단위, 회사코드·전표유형, 매핑 파일 해시, 전표 수·줄 수·차대 합계, 파일 이름과 SHA-256, 내보낸 사람), 전송 전표 `erp_export_docs`(파일에 든 증빙번호마다 한 행: 계좌, 전표일자), 전송 무효 `erp_export_voids`(ERP가 받지 않은 파일, 사유) 신설, 셋 다 수정·삭제·비우기 금지(테이블 27개). 무효가 아닌 내보내기에 이미 든 증빙번호와, 같은 계좌·같은 날을 다른 단위(건별 ↔ 일별 합계)로 낸 전표는 DB 트리거 `ministp_erp_doc_once`가 거부한다(계좌·날마다 잠그고 읽음). 뷰 `erp_exports_v`(무효 여부 포함) 추가. 공통코드 그룹 2개(ERP 전표 단위, ERP 전송 상태)와 감사 행위 `ERP_EXPORT`, `ERP_EXPORT_VOID` 추가(마이그레이션 `0011_erp_exports`). 기존 데이터는 바뀌지 않는다. |
| v2.12 | 2026-11-25 | 마감 점검 단계 기준과 점검 결과 이력(R-105, 초안). 단계 기준 `close_steps`(단계 코드마다 한 행: 이름, 순서, 사용 여부, 필수 여부, 상세 화면, 적용 자산 유형, 설명. 고칠 수 있지만 지우거나 코드를 바꾸지 못함: 트리거 `ministp_guard_close_steps`) 신설, 기본 단계 9개(결제, 주문·체결, 배당, 시세, 기준가, 증권사 대사, 일 마감, ERP 전송(선택), 원장 내부 대사)를 넣는다. 점검 결과 `close_checks`(점검할 때마다 계좌·영업일마다 한 행: 그날 상태, 끝난 필수 단계 수, 점검한 명령과 사람)와 `close_check_steps`(그 점검의 단계마다 한 행: 그때의 이름·필수 여부, 상태, 내용) 신설, 둘 다 수정·삭제·비우기 금지(테이블 29개). 뷰 `close_checks_last_v`(계좌·영업일마다 마지막 점검) 추가. 공통코드 그룹 2개(마감 점검 단계 상태, 마감 점검 일 상태)와 감사 행위 `CLOSE_STEP_SET` 추가(마이그레이션 `0012_close_steps`). 기존 데이터는 바뀌지 않는다. |
| v2.13 | 2026-11-25 | 계정과목표 v2: 법인 장부 기준(스펙 D4 v2, `docs/spec/05-chart-of-accounts-v2.md`, 초안). 계정과목 `accounts_chart`에 사용 여부 `in_use` 추가. 이름 변경(코드는 그대로): 1010 예수금 → 증권예치금, 1100 FVPL 주식 → FVPL 금융자산, 1110 FVPL 주식 평가조정 → FVPL 금융자산 평가조정, 3010 투자원금 → 본사 자금 대체, 4030 배당수익 → 배당금수익. 계정 추가: 1040 선납세금(원천징수 법인세, 자산), 4050 분배금수익(ETF 분배금). 5050 법인세등(원천징수)은 사용 중지(지난 전표가 가리키므로 행은 남김), 사용 중지한 계정으로는 새 분개 줄이 들어가지 않는다(트리거 `ministp_account_in_use`). 마이그레이션 `0013_chart_v2`(`changes-data: accounts_chart`, 이름과 사용 여부만 바뀌고 전표는 그대로). |
| v2.14 | 2026-11-26 | 고객을 법인으로(스펙 D7 v2, 11/26 사용자 결정, 초안). 계좌 위의 층은 리테일 위탁 고객이 아니라 장부를 가진 법인(회사)이다(D9). 이름 변경: `customers` → `companies`, `customer_id` → `company_id`(`companies`, `trading_accounts`, 뷰 `account_status_v`), 뷰 `customer_positions_v` → `company_positions_v`. 값과 규칙(C와 숫자 4자리)은 그대로이고 C는 Company로 읽는다. 법인 ERP 회사코드 이력 `company_erp_codes`(법인마다 코드와 적용시작일, 바꿀 때마다 한 행, 수정·삭제·비우기 금지, 테이블 30개) 신설. ERP 일반전표 파일의 회사코드는 줄마다 그 계좌 법인의 그날 코드, 없으면 매핑 파일의 값. 감사 행위 `COMPANY_ADD`, `COMPANY_ERP_CODE` 추가, `CUSTOMER_ADD`는 지난 기록용으로 이름만 바꿈. 마이그레이션 `0014_companies`(`renames` 머리말: 바뀐 이름으로 다시 지문을 떠서 기존 행이 그대로인지 확인, `changes-data: common_codes`). |
