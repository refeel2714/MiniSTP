# MiniSTP

A mini brokerage back office for Korean equities: straight-through processing from order to execution, T+2 settlement, an append-only ledger, double-entry journals, daily NAV, and automated daily reconciliation against broker balances.

주문부터 체결, 결제(T+2), 원장, 회계 분개, 일별 기준가, 증권사 잔고 일일 대사까지 한 흐름으로 처리하는 개인용 미니 증권사 원장 시스템입니다. 성공 기준은 수익률이 아니라 **원장이 틀리지 않는 것**입니다.

## 상태

Phase 0 (스펙 확정) 완료. Phase 1 (원장 코어 + 백테스트) 진행 예정.

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

## 보안

계좌번호, API 키, 실제 잔고 정보는 이 저장소에 커밋하지 않습니다.
