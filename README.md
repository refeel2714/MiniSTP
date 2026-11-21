# MiniSTP

A mini brokerage back office for Korean equities: straight-through processing from order to execution, T+2 settlement, an append-only ledger, double-entry journals, daily NAV, and automated daily reconciliation against broker balances.

주문부터 체결, 결제(T+2), 원장, 회계 분개, 일별 기준가, 증권사 잔고 일일 대사까지 한 흐름으로 처리하는 개인용 미니 증권사 원장 시스템입니다. 성공 기준은 수익률이 아니라 **원장이 틀리지 않는 것**입니다.

## 상태

Phase 0 (스펙 확정), Phase 1 (원장 코어 + 백테스트) 완료. Phase 2 (KIS 모의투자 운영) 진행 중: 주문·체결 반영, 일일 마감과 대사, 무오류 연속일 집계.

## 문서

- [스펙](docs/spec/README.md): 결정 사항, 도메인 규칙, 데이터 모델, 손계산 샘플 거래 세트

## 로드맵

| 단계 | 내용 | 완료 조건 |
|---|---|---|
| Phase 0 | 스펙 확정 | Markdown 스펙 v1.0 |
| Phase 1 | 원장 코어 + 백테스트 | 샘플 거래 세트 기대값 100% 일치 |
| Phase 2 | OMS + 모의투자 연동 (한국투자증권 KIS), 일일 마감과 대사 | 20영업일 연속 대사 무오류 |
| Phase 2.5 | 실시간 IBOR: 체결통보(WebSocket) 즉시 반영, 장중 포지션·주문가능금액, 장중 대사 | 모의 운영 중 장중 불일치 0건, 체결 반영 지연 측정 |
| Phase 3 | 계정계 확장: 다계좌(고객/자기 구분), 주문가능금액·증거금, 권리 처리(배당·분할), ABOR 일 마감(`DayClosed`), 감사 로그, 대시보드(IBOR 실시간 + ABOR 기준가) | 공개 저장소와 데모 완성 |
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
| `strategy`, `backtest` | 전략 인터페이스, 이벤트 기반 일봉 백테스트 (다음 봉 시가 체결, 슬리피지, 호가단위) | R-80~84 |
| `risk`, `ticks` | 사전 리스크 체크, KRX 호가단위 | R-82~83 |
| `strategies.ma_cross` | 예시 전략 (이동평균 교차) | D5 |
| `broker` | 증권사 인터페이스, 한국투자증권 KIS 조회 클라이언트(토큰 캐시, 잔고 연속조회), 대사 항목 변환 | D1, R-70~71 |
| `session` | 매매 세션: 리스크 체크 → OMS → 증권사 주문 → 체결 동기화(중복 없이) → 원장, 시스템 밖 주문 감지, 일일 대사 | R-40~43, R-70~74, R-83 |
| `ops` | 원장 시작값, 주문 기록, 일일 마감·대사, 무오류 연속일, 장중 IBOR 실행기(체결통보 즉시 반영, 조회 보완, 장중 대사) | R-05, R-44, R-74~75, D6 |
| `broker.kis_ws` | 실시간 체결통보 수신(접속키, 구독, AES 복호화, PINGPONG, 재연결) | D6 |
| `store` | PostgreSQL 이벤트 저장, 재생 복원, DB 수준 append-only·차대변 통제 | 03 데이터 모델, R-04, R-50 |

## 한국투자증권 모의투자 연결 확인 (본인 PC에서)

키는 저장소에 넣지 않고 환경변수로만 전달합니다. 아래 스크립트는 조회만 하고 주문은 내지 않으며, 키와 계좌번호를 출력하지 않습니다.

```bash
.venv/bin/python scripts/kis_smoke.py      # Windows: .venv\Scripts\python scripts\kis_smoke.py
```

앱키, 앱시크릿, 모의계좌번호(예: `12345678-01`)를 차례로 물어봅니다. 입력 내용은 화면에 표시되지 않고 저장되지 않습니다. `MINISTP_KIS_APP_KEY`, `MINISTP_KIS_APP_SECRET`, `MINISTP_KIS_ACCOUNT` 환경변수가 있으면 그 값을 씁니다.

기본 연결 대상은 모의투자 서버입니다. 실전 서버는 `MINISTP_KIS_ENV=live`와 `MINISTP_ALLOW_LIVE=1`을 둘 다 설정해야 열립니다(Phase 4 전용). 토큰은 `~/.ministp/kis_token.json`에 캐시합니다(KIS 토큰 발급 1분당 1회 제한).

### 주문 한 바퀴 확인 (장중, 모의투자 전용)

```bash
.venv/bin/python scripts/kis_order_check.py     # KODEX 200(069500) 1주 시장가 매수
```

빈 모의계좌에서 1주를 사고, 체결을 원장에 반영한 뒤 증권사 잔고와 대사 결과를 출력합니다. 실전 서버 설정에서는 실행되지 않습니다.

## 모의 운영 (Phase 2, 본인 PC에서)

원장과 주문, 대사 결과는 PostgreSQL에 쌓입니다. Windows에서는 [PostgreSQL 16 설치 파일](https://www.postgresql.org/download/windows/)로 설치하고, 설치 중 정한 `postgres` 계정 비밀번호를 명령 실행 때 입력합니다(`MINISTP_DATABASE_URL`을 설정하면 묻지 않습니다).

```bat
.venv\Scripts\python -m ministp db-init                       :: 처음 한 번
.venv\Scripts\python -m ministp instrument 069500 "KODEX 200" --etf
.venv\Scripts\python -m ministp open                          :: 처음 한 번, 미결제 거래가 없을 때
.venv\Scripts\python -m ministp run                           :: 장중 상시 실행 (실시간 체결 반영 + 주문 콘솔)
.venv\Scripts\python -m ministp order 069500 buy 1            :: run 없이 주문 1건만, --limit 가격 으로 지정가
.venv\Scripts\python -m ministp daily                         :: 영업일 15:40 이후
.venv\Scripts\python -m ministp status
.venv\Scripts\python -m ministp resolve 12 FEE_DIFF "메모"     :: 불일치 판단 기록
```

`run`은 한국투자증권 실시간 체결통보(WebSocket)를 받아 체결 즉시 원장(IBOR)에 반영하고, 끊기면 1분마다 조회로 보완하며, 5분마다 장중 대사를 남깁니다. 창에 `buy 069500 1`, `sell 069500 1 81000`, `status`, `recon`, `quit`을 입력해 씁니다. HTS ID(체결통보 구독 키)를 처음에 물어봅니다. 15:30에 스스로 끝나고, 원장을 쓰는 명령은 한 번에 하나만 실행됩니다.

`daily`는 결제, 체결 반영, 종가 평가와 NAV, 대사를 차례로 하고 무오류 연속 영업일을 보여줍니다. 증권사 앱에서 직접 낸 주문은 원장에 들어가지 않고 이상 거래로 표시됩니다.

## 보안

계좌번호, API 키, 실제 잔고 정보는 이 저장소에 커밋하지 않습니다.
