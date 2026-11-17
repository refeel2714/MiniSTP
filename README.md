# MiniSTP

A mini brokerage back office for Korean equities: straight-through processing from order to execution, T+2 settlement, an append-only ledger, double-entry journals, daily NAV, and automated daily reconciliation against broker balances.

주문부터 체결, 결제(T+2), 원장, 회계 분개, 일별 기준가, 증권사 잔고 일일 대사까지 한 흐름으로 처리하는 개인용 미니 증권사 원장 시스템입니다. 성공 기준은 수익률이 아니라 **원장이 틀리지 않는 것**입니다.

## 상태

Phase 0 (스펙 확정) 완료. Phase 1 (원장 코어 + 백테스트) 진행 중: 도메인 코어와 게이트 테스트 S1~S8 통과, PostgreSQL 원장 저장.

## 문서

- [스펙 v1.0](docs/spec/README.md): 결정 사항, 도메인 규칙, 데이터 모델, 손계산 샘플 거래 세트

## 로드맵

| 단계 | 내용 | 완료 조건 |
|---|---|---|
| Phase 0 | 스펙 확정 | Markdown 스펙 v1.0 |
| Phase 1 | 원장 코어 + 백테스트 | 샘플 거래 세트 기대값 100% 일치 |
| Phase 2 | OMS + 모의투자 연동 (한국투자증권 KIS) | 20영업일 연속 대사 무오류 |
| Phase 3 | 회계·성과·대시보드 | 공개 저장소와 데모 완성 |
| Phase 4~5 | 소액 실매매, 다자산 확장 (선택) | |

## 개발

```bash
uv venv && uv pip install -e . pytest
.venv/bin/pytest -q            # 전체
.venv/bin/pytest -q -m gate    # 스펙 04 게이트 테스트만
```

DB 테스트는 PostgreSQL 16이 필요합니다. `MINISTP_TEST_DATABASE_URL`이 없으면 건너뜁니다. 테스트는 해당 DB의 `public` 스키마를 지우고 다시 만들므로 전용 DB를 쓰세요.

```bash
MINISTP_TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/ministp_test .venv/bin/pytest -q -m db
```

| 모듈 | 역할 | 스펙 |
|---|---|---|
| `calendar` | 영업일, T+2 결제일 | R-10~11 |
| `fees` | 수수료, 증권거래세, 농특세 | R-20~22 |
| `ledger` | append-only 이벤트 원장, 포지션, 예수금, 결제, 분개, 평가, 기준가 | R-12~14, R-30~36, R-50~63 |
| `accounting` | 계정과목 v1, 차대변 검증 분개 | D4, R-50 |
| `oms` | 주문 상태 머신, 킬 스위치 | R-40~43 |
| `recon` | 일일 대사, 원인 후보 | R-70~74 |
| `broker` | 증권사 인터페이스, 한국투자증권 KIS 조회 클라이언트(토큰 캐시, 잔고 연속조회), 대사 항목 변환 | D1, R-70~71 |
| `store` | PostgreSQL 이벤트 저장, 재생 복원, DB 수준 append-only·차대변 통제 | 03 데이터 모델, R-04, R-50 |

## 한국투자증권 모의투자 연결 확인 (본인 PC에서)

키는 저장소에 넣지 않고 환경변수로만 전달합니다. 아래 스크립트는 조회만 하고 주문은 내지 않으며, 키와 계좌번호를 출력하지 않습니다.

```bash
export MINISTP_KIS_APP_KEY=...        # 모의투자 앱키
export MINISTP_KIS_APP_SECRET=...     # 모의투자 앱시크릿
export MINISTP_KIS_ACCOUNT=12345678-01  # 모의투자 계좌번호 (8자리-상품코드)
.venv/bin/python scripts/kis_smoke.py
```

기본 연결 대상은 모의투자 서버입니다. 실전 서버는 `MINISTP_KIS_ENV=live`와 `MINISTP_ALLOW_LIVE=1`을 둘 다 설정해야 열립니다(Phase 4 전용). 토큰은 `~/.ministp/kis_token.json`에 캐시합니다(KIS 토큰 발급 1분당 1회 제한).

## 보안

계좌번호, API 키, 실제 잔고 정보는 이 저장소에 커밋하지 않습니다.
