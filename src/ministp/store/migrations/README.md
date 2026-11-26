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
