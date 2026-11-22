-- title: 공통코드: 코드 그룹과 코드명
-- erd: 2.1
--
-- ERD v2.0 -> v2.1. 새 테이블 두 개와 코드 시드만 더한다. 기존 테이블과 기존 행은 건드리지 않는다.
-- 스키마 제약과 프로그램에 흩어져 있던 코드(계좌 상태, 대사 결과, 전표 유형 ...)에 코드명을 붙인다.
-- 계정과목처럼 원장이 직접 참조하는 코드는 전용 테이블(accounts_chart)에 그대로 둔다.

-- 1. 테이블 ------------------------------------------------------------------------------
create table code_groups (
    group_id    text primary key check (group_id ~ '^[A-Z][A-Z0-9_]{1,39}$'),
    name        text not null check (name <> ''),
    description text,
    used_by     text,
    fixed       boolean not null default true,
    created_at  timestamptz not null default now()
);

create table common_codes (
    group_id    text not null references code_groups (group_id),
    code        text not null check (code <> ''),
    name        text not null check (name <> ''),
    description text,
    sort_order  integer not null default 0,
    in_use      boolean not null default true,
    updated_at  timestamptz not null default now(),
    primary key (group_id, code)
);

-- 2. 코드는 이름을 바꾸거나 끌 수만 있다 (지우지 않는다) ------------------------------------
-- A code is renamed or switched off, never removed: old rows still carry it.
create or replace function ministp_guard_codes() returns trigger language plpgsql as $$
begin
    if tg_op = 'UPDATE' and tg_table_name = 'common_codes' then
        if new.group_id = old.group_id and new.code = old.code then
            new.updated_at := now();
            return new;
        end if;
    elsif tg_op = 'UPDATE' then
        if new.group_id = old.group_id and new.fixed = old.fixed then
            return new;
        end if;
    end if;
    raise exception 'code table %: % is not allowed (a code is renamed or switched off, never removed)',
        tg_table_name, tg_op using errcode = 'insufficient_privilege';
end $$;

create trigger code_groups_guard before update or delete on code_groups
    for each row execute function ministp_guard_codes();
create trigger code_groups_no_truncate before truncate on code_groups
    for each statement execute function ministp_guard_codes();
create trigger common_codes_guard before update or delete on common_codes
    for each row execute function ministp_guard_codes();
create trigger common_codes_no_truncate before truncate on common_codes
    for each statement execute function ministp_guard_codes();

-- 3. 한글 명칭 ---------------------------------------------------------------------------
comment on table code_groups is '공통코드 그룹';
comment on column code_groups.group_id is '코드 그룹 ID';
comment on column code_groups.name is '코드 그룹명';
comment on column code_groups.description is '코드 그룹 설명';
comment on column code_groups.used_by is '쓰는 곳 (이 코드를 저장하는 테이블.컬럼)';
comment on column code_groups.fixed is '코드 고정 여부 (프로그램이 정한 코드는 이름만 바꿀 수 있음)';
comment on column code_groups.created_at is '등록 시각';

comment on table common_codes is '공통코드 (코드와 코드명)';
comment on column common_codes.group_id is '코드 그룹 ID';
comment on column common_codes.code is '코드';
comment on column common_codes.name is '코드명';
comment on column common_codes.description is '코드 설명';
comment on column common_codes.sort_order is '표시 순서';
comment on column common_codes.in_use is '사용 여부';
comment on column common_codes.updated_at is '수정 시각';

-- 4. 코드 시드 (ministp.codes.SEED와 같다. tests/db/test_codes.py가 확인) ---------------------
insert into code_groups (group_id, name, description, used_by, fixed) values
    ('ACCOUNT_STATUS', '계좌 상태', '위탁계좌의 상태. 정상 계좌만 주문을 받아요 (R-39).', 'account_status_history.status', true),
    ('ACCOUNT_ENV', '계좌 환경', '증권사 모의투자 계좌인지 실전 계좌인지.', 'trading_accounts.env', true),
    ('ACCOUNT_TYPE', '계좌 유형', '위탁계좌의 종류.', 'trading_accounts.account_type', true),
    ('BROKER', '증권사', '주문을 내고 잔고를 받아 오는 증권사.', 'trading_accounts.broker', true),
    ('MARKET', '시장', '종목이 상장된 시장.', 'instruments.market', true),
    ('ASSET_TYPE', '자산 유형', '종목의 종류. 매도 세율이 여기서 갈려요.', 'instruments.asset_type', true),
    ('ORDER_SIDE', '매매 구분', '주문과 체결의 방향.', 'oms_orders.side', true),
    ('ORDER_ACK', '주문 접수 결과', '증권사가 주문을 받았는지.', 'oms_order_acks.outcome', true),
    ('ORDER_STATUS', '주문 상태', '주문 하나가 거치는 상태 (주문·체결 화면).', null, true),
    ('ORDER_REJECT', '주문 전 검증 거부 사유', '주문을 내기 전에 시스템이 막는 이유 (R-36~39).', null, true),
    ('RECON_STATUS', '대사 결과', '원장 값과 증권사 값을 맞춰 본 결과.', 'recon_results.status', true),
    ('RECON_ITEM', '대사 항목', '무엇을 맞춰 보는지.', 'recon_results.item', true),
    ('RECON_CAUSE', '대사 불일치 원인', '불일치의 원인 분류 (R-73). 최종 판단은 사람이 해요.', 'recon_results.cause_code', true),
    ('SNAPSHOT_KIND', '증권사 응답 종류', '증권사 응답 원본을 받은 계기.', 'broker_snapshots.kind', true),
    ('EVENT_TYPE', '원장 이벤트 유형', '원장에 쌓이는 이벤트의 종류. 모든 기록의 원천이에요.', 'ledger_events.event_type', true),
    ('JOURNAL_KIND', '전표 유형', '분개 전표가 만들어진 이유.', 'journal_entries.kind', true),
    ('CASH_REASON', '예수금 변동 사유', '예수금이 움직인 이유. 예수금은 결제일에만 변해요 (R-13).', 'cash_ledger.reason', true),
    ('NORMAL_SIDE', '정상잔액 방향', '계정과목의 잔액이 늘어나는 쪽.', 'accounts_chart.normal_side', true);
insert into common_codes (group_id, code, name, description, sort_order) values
    ('ACCOUNT_STATUS', 'OPEN', '정상', '주문, 결제, 마감, 대사를 모두 해요', 1),
    ('ACCOUNT_STATUS', 'SUSPENDED', '거래정지', '새 주문만 막고 결제, 마감, 대사는 계속해요', 2),
    ('ACCOUNT_STATUS', 'CLOSED', '해지', '잔고가 0일 때만 가능하고 다시 열지 않아요', 3),
    ('ACCOUNT_ENV', 'PAPER', '모의투자', 'Phase 4 전까지는 모의투자만 등록해요', 1),
    ('ACCOUNT_ENV', 'LIVE', '실전', '실계좌. 이중 잠금을 풀어야 주문이 나가요', 2),
    ('ACCOUNT_TYPE', 'GENERAL', '위탁종합', '국내 주식·ETF 현물을 거래하는 일반 위탁계좌', 1),
    ('BROKER', 'KIS', '한국투자증권', 'KIS Developers Open API', 1),
    ('MARKET', 'KOSPI', '유가증권시장', null, 1),
    ('MARKET', 'KOSDAQ', '코스닥시장', null, 2),
    ('ASSET_TYPE', 'STOCK', '주식', '매도할 때 증권거래세와 농어촌특별세가 붙어요', 1),
    ('ASSET_TYPE', 'ETF', '상장지수펀드', '매도할 때 거래세가 없어요', 2),
    ('ORDER_SIDE', 'BUY', '매수', null, 1),
    ('ORDER_SIDE', 'SELL', '매도', null, 2),
    ('ORDER_ACK', 'ACCEPTED', '접수', '증권사 주문번호가 있어요', 1),
    ('ORDER_ACK', 'REJECTED', '거부', '증권사가 받지 않았어요. 사유는 응답 내용에 남아요', 2),
    ('ORDER_STATUS', 'NEW', '신규', '주문 의도를 저장했고 아직 증권사에 내지 않았어요', 1),
    ('ORDER_STATUS', 'SUBMITTED', '접수', '증권사가 받았어요', 2),
    ('ORDER_STATUS', 'PARTIALLY_FILLED', '부분체결', null, 3),
    ('ORDER_STATUS', 'FILLED', '체결완료', null, 4),
    ('ORDER_STATUS', 'CANCEL_PENDING', '취소 중', '취소를 냈고 결과를 기다려요', 5),
    ('ORDER_STATUS', 'CANCELED', '취소', null, 6),
    ('ORDER_STATUS', 'REJECTED', '거부', null, 7),
    ('ORDER_STATUS', 'REPLACED', '정정', null, 8),
    ('ORDER_REJECT', 'KILL_SWITCH', '킬 스위치', '킬 스위치가 켜져 있어 모든 주문을 막아요', 1),
    ('ORDER_REJECT', 'ACCOUNT_NOT_OPEN', '정상 계좌 아님', '정지·해지 계좌는 주문을 받지 않아요 (R-39)', 2),
    ('ORDER_REJECT', 'NOT_BUSINESS_DAY', '영업일 아님', '휴장일에는 주문을 내지 않아요 (R-38)', 3),
    ('ORDER_REJECT', 'BAD_QTY', '수량 오류', '수량은 1주 이상', 4),
    ('ORDER_REJECT', 'BAD_TICK', '호가단위 오류', '가격이 호가단위에 맞지 않아요 (R-38)', 5),
    ('ORDER_REJECT', 'MAX_ORDER_VALUE', '1회 주문 한도 초과', null, 6),
    ('ORDER_REJECT', 'SHORT_SELL', '매도가능수량 초과', '공매도 금지 (R-36)', 7),
    ('ORDER_REJECT', 'INSUFFICIENT_CASH', '주문가능금액 부족', null, 8),
    ('ORDER_REJECT', 'NO_PRICE', '기준 단가 없음', '지정가도 최근 종가도 없어요', 9),
    ('RECON_STATUS', 'MATCH', '일치', null, 1),
    ('RECON_STATUS', 'BREAK', '불일치', '원인을 판단해 해결 행을 남겨야 무오류일이 돼요 (R-74)', 2),
    ('RECON_STATUS', 'RESOLVED', '해결', '불일치 행을 고치지 않고 판단 행을 덧붙여요', 3),
    ('RECON_ITEM', 'CASH_D0', '예수금', '오늘 쓸 수 있는 예수금', 1),
    ('RECON_ITEM', 'CASH_PROJECTED', '결제 후 예수금', '미결제 체결이 모두 결제된 뒤의 예수금', 2),
    ('RECON_ITEM', 'POSITION_QTY', '보유수량', '종목별', 3),
    ('RECON_ITEM', 'BOOK_COST', '장부금액', '종목별 매입금액', 4),
    ('RECON_ITEM', 'EXEC_QTY', '체결수량', '장중 대사: 그날 체결된 수량', 5),
    ('RECON_CAUSE', 'MISSING_EXEC', '체결 누락', '원장에 없는 체결이 증권사에 있어요', 1),
    ('RECON_CAUSE', 'FEE_DIFF', '수수료 차이', null, 2),
    ('RECON_CAUSE', 'TAX_DIFF', '세금 차이', null, 3),
    ('RECON_CAUSE', 'ROUNDING', '단수 차이', '건별 계산과 합산 계산의 1원 단위 차이 (R-22)', 4),
    ('RECON_CAUSE', 'CORP_ACTION', '권리 발생', '배당, 증자, 분할 같은 권리 반영 시점 차이', 5),
    ('RECON_CAUSE', 'TIMING', '시점 차이', '조회 시점이 달라 생긴 차이', 6),
    ('RECON_CAUSE', 'UNKNOWN', '원인 미상', '이 원인이 남아 있으면 무오류일이 아니에요', 7),
    ('SNAPSHOT_KIND', 'OPENING_BALANCE', '시작 잔고', '원장을 열 때 받은 잔고', 1),
    ('SNAPSHOT_KIND', 'DAILY_RECON', '일일 대사', '장 마감 뒤 대사에 쓴 잔고', 2),
    ('SNAPSHOT_KIND', 'INTRADAY_RECON', '장중 대사', '장중에 체결을 맞춰 본 응답', 3),
    ('SNAPSHOT_KIND', 'FILL_NOTICE', '체결통보', '실시간 체결통보 원본', 4),
    ('EVENT_TYPE', 'DepositEvent', '입금', '투자원금 입금', 1),
    ('EVENT_TYPE', 'OpeningPositionEvent', '시작 보유', '원장을 열 때 이미 가지고 있던 종목', 2),
    ('EVENT_TYPE', 'ExecutionEvent', '체결', '매수·매도 체결', 3),
    ('EVENT_TYPE', 'SettlementEvent', '결제', '체결 대금이 예수금에 반영되는 날', 4),
    ('EVENT_TYPE', 'RevaluationEvent', '평가', '종가로 다시 평가', 5),
    ('EVENT_TYPE', 'NavEvent', '기준가', '하루의 기준가 산출', 6),
    ('JOURNAL_KIND', 'DEPOSIT', '입금', null, 1),
    ('JOURNAL_KIND', 'OPENING', '시작 보유', null, 2),
    ('JOURNAL_KIND', 'EXECUTION', '체결', '체결일에 미수금·미지급금으로 인식', 3),
    ('JOURNAL_KIND', 'SETTLEMENT', '결제', '결제일에 예수금으로 정리', 4),
    ('JOURNAL_KIND', 'VALUATION', '평가', '종가 평가손익', 5),
    ('JOURNAL_KIND', 'VALUATION_REVERSAL', '평가 환입', '전일 평가분을 되돌림', 6),
    ('CASH_REASON', 'DEPOSIT', '입금', null, 1),
    ('CASH_REASON', 'SETTLEMENT', '결제', '매수 대금 지급 또는 매도 대금 수령', 2),
    ('NORMAL_SIDE', 'D', '차변', '자산, 비용', 1),
    ('NORMAL_SIDE', 'C', '대변', '부채, 자본, 수익', 2);
