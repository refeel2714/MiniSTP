# 마이그레이션

기존 DB를 다음 ERD 버전으로 올리는 SQL 파일을 여기에 둡니다. 새 DB는 지금처럼 `schema.sql`로 만듭니다(`db-init`).

- 파일 이름: `NNNN_이름.sql` (0001부터 빈 번호 없이)
- 머리 주석: `-- title: 설명`, `-- erd: 2.0`, 필요하면 `-- changes-data: 테이블`
- 실행: `python -m ministp db-migrate` (상태와 계획), `--dry-run` (적용해 보고 원장 무결성까지 점검한 뒤 되돌림), `--apply` (리허설이 통과해야 적용)
- 적용된 파일은 고치지 않습니다. 고칠 일이 있으면 새 번호로 추가합니다.

마이그레이션마다 적용 전후로 모든 테이블의 지문(행 수, 기존 컬럼 기준 행 해시)을 비교합니다. 기존 행이 바뀌면 전체를 되돌립니다.

새 DB를 만드는 `schema.sql`과 여기 있는 파일은 같은 스키마에 도착해야 합니다. v1.1 DB의 덤프에 마이그레이션을 적용한 결과와 새 DB를 비교하는 테스트(`tests/db/test_multi_account.py`)가 확인합니다. 그래서 스키마를 바꿀 때는 `schema.sql`과 마이그레이션 파일을 함께 고칩니다.

| 번호 | ERD | 내용 |
|---|---|---|
| (기준) | v1.1 | 마이그레이션 도구 이전의 스키마 |
| 0001_multi_account | v2.0 | 다계좌. 고객·위탁계좌·상태 이력 테이블을 만들고, 원장·주문·대사 7개 테이블에 `account_id`를 붙여 기존 행을 기본 계좌 `A0001`로 옮깁니다. 기존 행의 기존 컬럼은 바꾸지 않습니다. |
| 0002_common_codes | v2.1 | 공통코드. 코드 그룹 `code_groups`와 코드 `common_codes`를 만들고 18개 그룹의 코드명을 넣습니다. 기존 테이블과 기존 행은 건드리지 않습니다. |
| 0003_day_close | v2.2 | 일 마감. 마감 기록 `day_closes`, 마감일 잠금 트리거(전표·예수금·마감 행), 뷰 세 개(`last_close_v`, `abor_positions_v`, `unclosed_events_v`)를 만들고 공통코드에 코드 세 개를 더합니다(`changes-data: common_codes`, 기존 행은 그대로). 마감한 날이 아직 없으므로 적용 직후에는 아무것도 잠기지 않습니다. |
| 0004_audit_log | v2.3 | 감사 로그. `audit_log`와 해시 사슬 트리거(`ministp_audit_chain`, `ministp_audit_hash`)를 만들고 공통코드 그룹 세 개(경로, 결과, 행위)와 그 코드를 넣습니다(`changes-data: code_groups, common_codes`, 기존 행은 그대로). 적용이 끝나면 그 적용이 감사 로그의 첫 줄로 남습니다. |
| 0005_corporate_actions | v2.4 | 권리처리(현금배당). 권리 원장 `corporate_actions`와 뷰 두 개(`corporate_actions_v`, `dividends_v`)를 만들고 공통코드 그룹 두 개와 코드를 더합니다(`changes-data: code_groups, common_codes`, 기존 행은 그대로). 배당 인식·입금은 원장 이벤트로 남아서 기존 테이블에 행이 늘 뿐, 새 프로젝션 테이블은 없습니다. |
| 0006_cash_movements | v2.5 | 입출금·대체. 뷰 두 개(`cash_movements_v`, `transfers_v`)와 `ledger_events`의 부분 유일 인덱스(대체 번호)를 만들고 공통코드 12개를 더합니다(`changes-data: common_codes`, 기존 행은 그대로). 테이블은 늘지 않습니다. 출금과 대체는 원장 이벤트로 남고 분개와 예수금 행은 기존 테이블에 들어갑니다. |
| 0007_adjustments | v2.6 | 수동 조정. 뷰 `adjustments_v`와 `ledger_events`의 부분 유일 인덱스(조정 번호)를 만들고, 계정과목 세 개(이자수익 4040, 잡이익 4090, 잡손실 5090), 공통코드 그룹 하나(조정 사유)와 코드 14개를 더합니다(`changes-data: accounts_chart, code_groups, common_codes`, 기존 행은 그대로). 테이블은 늘지 않습니다. |
| 0008_instrument_master | v2.7 | 종목 마스터 이력. 적재 기록 `master_loads`와 이력 `instrument_master`(둘 다 수정·삭제 금지), 뷰 두 개(`instrument_master_current_v`, `instrument_master_match_v`)를 만들고 공통코드 그룹 두 개(증권그룹 구분, 대조 결과)와 감사 행위 코드 하나를 더합니다(`changes-data: code_groups, common_codes`, 기존 행은 그대로). 적용 직후에는 이력이 비어 있고, `master load`가 받은 파일을 넣습니다. |
| 0009_order_approval | v2.9 | 주문 결재. 사용자 `app_users`, 결재 권한 이력 `user_rights`, 주문 요청 `order_requests`(키: 주문일 + 요청 번호, 주문 유형)와 요청 줄 `order_request_lines`, 결재 이력 `order_approvals`(요청 단위), 전송 이력 `order_sends`(줄 단위)(여섯 다 수정·삭제 금지), 트리거 셋(결재 순서, 요청 뒤 줄 고정, 전송 순서), 뷰 세 개(`user_rights_v`, `order_requests_v`, `order_request_lines_v`)를 만들고 공통코드 그룹 다섯 개(결재 권한, 결재 행위, 주문 요청 상태, 주문 유형, 전송 결과)와 감사 행위 코드 일곱 개를 더합니다(`changes-data: code_groups, common_codes`, 기존 행은 그대로). 적용 직후에는 사용자가 없어서 주문을 낼 수 없습니다. `user add`와 `right grant`로 먼저 권한을 정합니다. |
| 0010_send_unknown | v2.10 | 주문 결과 미확인 처리(R-103). 테이블은 그대로이고 주문 요청 뷰 두 개(`order_requests_v`, `order_request_lines_v`)가 증권사 접수 결과(`oms_order_acks`)를 함께 읽어 결과 미확인·거부 줄을 구분합니다. 공통코드 두 개(요청 상태 결과 미확인, 감사 행위 주문 결과 확인)를 더합니다(`changes-data: common_codes`, 기존 행은 그대로). |
| 0011_erp_exports | v2.11 | ERP 전송 이력(R-104). 내보낸 파일 `erp_exports`, 그 안의 전표 `erp_export_docs`, ERP가 받지 않은 파일의 무효 `erp_export_voids`(셋 다 수정·삭제 금지), 같은 전표를 두 번 내지 않게 하는 트리거(`ministp_erp_doc_once`), 뷰 `erp_exports_v`를 만들고 공통코드 그룹 두 개(ERP 전표 단위, ERP 전송 상태)와 감사 행위 코드 두 개를 더합니다(`changes-data: code_groups, common_codes`, 기존 행은 그대로). 적용 직후에는 이력이 비어 있어서, 그 전에 손으로 올린 전표는 시스템이 모릅니다. 첫 내보내기는 아직 ERP에 없는 날부터 하세요. |
| 0012_close_steps | v2.12 | 마감 점검 단계 기준과 점검 결과 이력(R-105). 단계 기준 `close_steps`(고치되 지우지 않음, 기본 단계 9개를 넣음), 점검 결과 `close_checks`와 단계별 결과 `close_check_steps`(둘 다 수정·삭제 금지), 단계 기준 보호 트리거(`ministp_guard_close_steps`), 뷰 `close_checks_last_v`를 만들고 공통코드 그룹 두 개(단계 상태, 일 상태)와 감사 행위 코드 하나를 더합니다(`changes-data: code_groups, common_codes`, 기존 행은 그대로). 적용 전의 날은 점검 이력이 없고, 화면(STP-2400)은 지금처럼 그때그때 다시 계산해 보여 줍니다. |
| 0013_chart_v2 | v2.13 | 계정과목표 v2(법인 장부 기준). `accounts_chart`에 사용 여부 열을 더하고, 이름 다섯 개를 바꾸고(1010 증권예치금, 1100 FVPL 금융자산, 1110 FVPL 금융자산 평가조정, 3010 본사 자금 대체, 4030 배당금수익, 코드는 그대로), 1040 선납세금·4050 분배금수익을 더하고, 5050 법인세등(원천징수)을 사용 중지합니다. 사용 중지 계정으로 새 분개 줄이 들어가지 않게 하는 트리거(`ministp_account_in_use`)를 만듭니다(`changes-data: accounts_chart`). 전표는 바뀌지 않습니다. 운영 DB에는 5050이 쓰인 적이 없습니다. |
