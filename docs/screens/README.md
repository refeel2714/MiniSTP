# STP-UI 화면 목록과 영향도

> `python scripts/gen_screen_docs.py`로 생성돼요. 화면 정의(YAML)와 DB 주석에서 만들어지니 직접 고치지 마세요.

## 화면 분류

화면번호 `STP-ABCC`가 곧 분류예요. 첫 자리 A는 대분류(거래가 흘러가는 순서), 둘째 자리까지 AB는 중분류(그 단계에서 다루는 대상), 뒤 두 자리 CC는 같은 대상을 다루는 화면의 순번이에요. 조회와 처리는 번호가 아니라 트랜잭션 ID로 갈라요(R 조회, U 처리). 분류는 [menu.yaml](../../src/ministp/ui/menu.yaml) 한 곳에 적고, 서버는 시작할 때 모든 화면번호가 분류에 들어가는지, 화면이 없는 분류가 없는지 확인해요. 왼쪽 메뉴트리와 첫 화면도 이 표를 그대로 그려요.

| 대분류 | 중분류 | 화면 |
|---|---|---|
| **1 주문·잔고**<br>주문에서 체결까지, 그리고 지금 들고 있는 것 (IBOR) | 11 잔고 | [STP-1100](STP-1100.md) 실시간 잔고 |
|  | 12 주문·체결 | [STP-1200](STP-1200.md) 주문·체결 블로터 |
|  | 13 주문가능 | [STP-1300](STP-1300.md) 주문가능 조회 |
|  | 15 운용 주문 | [STP-1500](STP-1500.md) 바스켓 주문 시뮬레이션 |
| **2 대사·평가**<br>증권사와 맞는지, 하루의 값이 얼마인지 | 21 대사 | [STP-2100](STP-2100.md) 일일 대사 결과, [STP-2110](STP-2110.md) 원장 내부 대사 |
|  | 22 기준가 | [STP-2200](STP-2200.md) 기준가·무오류 현황, [STP-2210](STP-2210.md) 기준가 이력 |
|  | 23 시세 | [STP-2300](STP-2300.md) 시세 추이 |
|  | 24 마감 점검 | [STP-2400](STP-2400.md) 마감 점검 |
| **3 원장·회계**<br>기록된 이벤트와 그 분개, 거기서 나온 계좌 현황 (ABOR) | 31 원장 | [STP-3100](STP-3100.md) 원장 조회 |
|  | 32 분개 | [STP-3200](STP-3200.md) 분개 조회 |
|  | 33 시산표 | [STP-3300](STP-3300.md) 시산표 |
|  | 34 계좌 현황 | [STP-3400](STP-3400.md) 계좌정보 |
| **4 기준정보**<br>위 세 단계가 함께 쓰는, 계좌와 무관한 종목과 거래 기준 | 41 종목 | [STP-4100](STP-4100.md) 종목정보 |
|  | 43 거래 기준 | [STP-4300](STP-4300.md) 거래 기준 |
| **6 성과**<br>원장과 기준가에서 나온 결과. 입출금은 좌수로 들어가서 기준가 수익률에 섞이지 않아요 | 61 수익률 | [STP-6100](STP-6100.md) 수익률 |

## 화면

| 화면번호 | 화면명 | 그리드 | 트랜잭션 |
|---|---|---|---|
| [STP-1100](STP-1100.md) | 실시간 잔고 | 2 | `STP1100R02`, `STP1100R01` |
| [STP-1200](STP-1200.md) | 주문·체결 블로터 | 3 | `STP1200R01`, `STP1200R02` |
| [STP-1300](STP-1300.md) | 주문가능 조회 | 2 | `STP1300R02`, `STP1300R01` |
| [STP-1500](STP-1500.md) | 바스켓 주문 시뮬레이션 | 3 | `STP1500R01`, `STP1500R02` |
| [STP-2100](STP-2100.md) | 일일 대사 결과 | 2 | `STP2100R02`, `STP2100R01`, `STP2100U01` |
| [STP-2110](STP-2110.md) | 원장 내부 대사 | 3 | `STP2110R01`, `STP2110R02` |
| [STP-2200](STP-2200.md) | 기준가·무오류 현황 | 1 | `STP2200R01` |
| [STP-2210](STP-2210.md) | 기준가 이력 | 2 | `STP2210R01`, `STP2210R02` |
| [STP-2300](STP-2300.md) | 시세 추이 | 2 | `STP2300R01`, `STP2300R02` |
| [STP-2400](STP-2400.md) | 마감 점검 | 2 | `STP2400R01`, `STP2400R02` |
| [STP-3100](STP-3100.md) | 원장 조회 | 3 | `STP3100R01`, `STP3100R02` |
| [STP-3200](STP-3200.md) | 분개 조회 | 2 | `STP3200R01`, `STP3200R02` |
| [STP-3300](STP-3300.md) | 시산표 | 1 | `STP3300R01` |
| [STP-3400](STP-3400.md) | 계좌정보 | 3 | `STP3400R01`, `STP3400R02`, `STP3400R03` |
| [STP-4100](STP-4100.md) | 종목정보 | 2 | `STP4100R01`, `STP4100R02` |
| [STP-4300](STP-4300.md) | 거래 기준 | 5 | `STP4300R01`, `STP4300R02`, `STP4300R03`, `STP4300R04`, `STP4300R05` |
| [STP-6100](STP-6100.md) | 수익률 | 2 | `STP6100R01`, `STP6100R02` |

## 바로 가기 창 (Ctrl+K)

어느 화면에서나 Ctrl+K로 열어 화면번호나 화면명을 넣고 Enter로 옮겨 가요. 조회조건이 찾기 팝업으로 받는 코드는 그 코드로 화면을 바로 열 수 있어요(여러 화면이 받으면 다음 단계에서 화면을 골라요).

| 찾는 것 | 목록을 주는 트랜잭션 | 열 수 있는 화면 (넘기는 조회조건) |
|---|---|---|
| 화면 | - | 화면 17개 모두 |
| 종목 | `STP4100R01` | STP-1300 주문가능 조회(`symbol`), STP-3100 원장 조회(`symbol`), STP-3200 분개 조회(`symbol`) |

## 영향도: DB 컬럼 → 화면

스키마를 바꾸기 전에 이 표에서 그 컬럼을 쓰는 화면을 확인해요.

| DB 컬럼 | 한글명 | 쓰는 화면·그리드 |
|---|---|---|
| `accounts_chart.code` | 계정코드 | STP-2210 bridge, STP-3300 trial |
| `accounts_chart.name` | 계정명 | STP-2210 bridge, STP-3200 journal, STP-3300 trial |
| `broker_snapshots.kind` | 유형 | STP-2100 days, STP-2100 조회조건 |
| `broker_snapshots.taken_at` | 수신 시각 | STP-2100 results |
| `cash_ledger.amount` | 금액 | STP-3100 cash |
| `cash_ledger.date` | 일자 | STP-3100 cash |
| `cash_ledger.reason` | 사유 | STP-3100 cash |
| `cash_ledger.ref_id` | 참조 ID | STP-3100 cash |
| `instruments.asset_type` | 자산 유형 | STP-1100 holdings, STP-1200 orders, STP-1300 symbols, STP-1500 basket, STP-1500 plan, STP-2300 symbols, STP-3400 settlements, STP-4100 instruments, STP-4300 fees, STP-4300 ticks |
| `instruments.market` | 시장 | STP-4100 instruments, STP-4300 fees |
| `instruments.name` | 종목명 | STP-1100 holdings, STP-1200 orders, STP-1300 symbols, STP-1500 basket, STP-1500 plan, STP-2300 symbols, STP-3400 settlements, STP-4100 instruments |
| `instruments.symbol` | 종목코드 | STP-1100 holdings, STP-1300 symbols, STP-1300 조회조건, STP-1500 basket, STP-1500 plan, STP-2300 symbols, STP-3100 events, STP-3100 조회조건, STP-3200 events, STP-3200 조회조건, STP-3400 settlements, STP-4100 instruments |
| `journal_entries.date` | 회계일 | STP-3200 journal |
| `journal_entries.entry_id` | 전표 번호 | STP-3200 journal |
| `journal_entries.kind` | 전표 유형 | STP-3200 journal |
| `journal_entries.reverses_entry_id` | 역분개 대상 전표 번호 | STP-3200 journal |
| `journal_entries.source_id` | 원천 참조 | STP-3200 journal |
| `journal_lines.account_code` | 계정코드 | STP-3200 journal |
| `journal_lines.credit` | 대변 금액 | STP-3200 journal |
| `journal_lines.debit` | 차변 금액 | STP-3200 journal |
| `ledger_events.event_type` | 이벤트 유형 | STP-3100 events, STP-3100 조회조건, STP-3200 events, STP-3200 조회조건 |
| `ledger_events.payload` | 이벤트 내용 | STP-3100 events |
| `ledger_events.recorded_at` | 기록 시각 | STP-3100 events |
| `ledger_events.seq` | 이벤트 순번 | STP-3100 events, STP-3100 조회조건, STP-3200 events, STP-3200 조회조건 |
| `oms_order_acks.detail` | 거부 사유 | STP-1200 order |
| `oms_order_acks.order_no` | 증권사 주문번호 | STP-1200 order |
| `oms_order_acks.outcome` | 결과 | STP-1200 order |
| `oms_orders.client_id` | 내부 주문번호 | STP-1200 orders |
| `oms_orders.created_at` | 생성 시각 | STP-1200 orders |
| `oms_orders.limit_price` | 지정가 | STP-1200 order, STP-1300 조회조건 |
| `oms_orders.qty` | 주문수량 | STP-1200 orders, STP-1300 조회조건 |
| `oms_orders.side` | 매매구분 | STP-1200 orders, STP-1300 조회조건, STP-1500 조회조건, STP-3400 settlements |
| `oms_orders.symbol` | 종목코드 | STP-1200 orders |
| `oms_orders.trade_date` | 주문일 | STP-1200 조회조건 |
| `position_ledger.cost_delta` | 장부금액 증감 | STP-3100 positions |
| `position_ledger.exec_id` | 체결 ID | STP-1200 fills, STP-3100 positions |
| `position_ledger.qty_delta` | 수량 증감 | STP-3100 positions |
| `position_ledger.realized_pnl` | 실현손익 | STP-3100 positions |
| `position_ledger.symbol` | 종목코드 | STP-3100 positions |
| `position_ledger.trade_date` | 매매일 | STP-3100 positions |
| `positions_v.book_cost` | 장부금액 | STP-1100 holdings |
| `positions_v.qty` | 보유수량 | STP-1100 holdings, STP-1300 symbols |
| `recon_results.broker_value` | 증권사 값 | STP-2100 results |
| `recon_results.cause_code` | 원인 코드 | STP-2100 results, STP-2100 results 버튼 resolve |
| `recon_results.date` | 기준일 | STP-2100 days |
| `recon_results.diff` | 차이 | STP-2100 results |
| `recon_results.id` | 행 번호 | STP-2100 results, STP-2100 results 버튼 resolve |
| `recon_results.internal_value` | 내부(원장) 값 | STP-2100 results |
| `recon_results.item` | 대사 항목 | STP-2100 results |
| `recon_results.key` | 항목 키 | STP-2100 results |
| `recon_results.note` | 판단 메모 | STP-2100 results 버튼 resolve |
| `recon_results.status` | 상태 | STP-2100 results |
