-- title: 일 마감: 마감 기록과 마감일 잠금, ABOR 뷰
-- erd: 2.2
-- changes-data: common_codes
--
-- ERD v2.1 -> v2.2 (스펙 D6, R-77~79). 새 테이블 하나, 잠금 트리거, 뷰 세 개, 코드 세 개를 더한다.
-- 기존 테이블의 기존 행은 건드리지 않는다. common_codes는 새 코드 세 행이 늘어나서 위에 적었다.
-- 마감한 날이 아직 없으므로 잠금 트리거는 적용 직후에는 아무것도 막지 않는다.

-- 1. 일 마감 ----------------------------------------------------------------------------
create table day_closes (
    account_id   text not null,
    date         date not null,
    event_seq    bigint not null,
    net_assets   bigint not null,
    units        bigint not null check (units >= 0),
    nav_per_1000 numeric(18, 2) not null,
    closed_at    timestamptz not null default now(),
    primary key (account_id, date),
    constraint day_closes_event_key unique (event_seq),
    constraint day_closes_event_account_fkey
        foreign key (event_seq, account_id) references ledger_events (seq, account_id)
);

-- 2. 고칠 수 없는 기록이고 (R-04), 마감일 안으로는 아무것도 더 들어가지 않는다 (R-78) -------------
create trigger day_closes_append_only before update or delete on day_closes
    for each row execute function ministp_forbid_change();
create trigger day_closes_no_truncate before truncate on day_closes
    for each statement execute function ministp_forbid_change();

create or replace function ministp_forbid_closed_day() returns trigger language plpgsql as $$
begin
    if exists (select 1 from day_closes c where c.account_id = new.account_id and c.date >= new.date) then
        raise exception '%: % is inside a closed day of account % (book a correction on the open day)',
            tg_table_name, new.date, new.account_id using errcode = 'check_violation';
    end if;
    return new;
end $$;

create trigger journal_entries_closed_day before insert on journal_entries
    for each row execute function ministp_forbid_closed_day();
create trigger cash_ledger_closed_day before insert on cash_ledger
    for each row execute function ministp_forbid_closed_day();
create trigger day_closes_closed_day before insert on day_closes
    for each row execute function ministp_forbid_closed_day();

-- 3. 뷰: 마지막 마감, 그 시점의 보유(ABOR), 그 뒤의 이벤트 -----------------------------------
create view last_close_v as
select distinct on (account_id) account_id, date, event_seq, net_assets, units, nav_per_1000, closed_at
  from day_closes order by account_id, date desc;

create view abor_positions_v as
select c.account_id, c.date as closed_date, p.symbol, sum(p.qty_delta) as qty, sum(p.cost_delta) as book_cost
  from last_close_v c join position_ledger p on p.account_id = c.account_id and p.event_seq <= c.event_seq
 group by c.account_id, c.date, p.symbol having sum(p.qty_delta) <> 0;

create view unclosed_events_v as
select e.account_id, e.seq, e.event_type, e.recorded_at
  from ledger_events e left join last_close_v c on c.account_id = e.account_id
 where c.event_seq is null or e.seq > c.event_seq;

-- 4. 한글 명칭 ---------------------------------------------------------------------------
comment on table day_closes is '일 마감 (마감 이벤트마다 한 행, 그날의 확정 기준가)';
comment on column day_closes.account_id is '계좌 ID';
comment on column day_closes.date is '마감일 (이 날짜까지 장부 마감)';
comment on column day_closes.event_seq is '마감 이벤트 순번';
comment on column day_closes.net_assets is '마감 순자산';
comment on column day_closes.units is '마감 좌수';
comment on column day_closes.nav_per_1000 is '마감 기준가 (1,000좌당)';
comment on column day_closes.closed_at is '마감 기록 시각';

comment on view last_close_v is '계좌별 마지막 마감';
comment on column last_close_v.account_id is '계좌 ID';
comment on column last_close_v.date is '마지막 마감일';
comment on column last_close_v.event_seq is '마감 이벤트 순번';
comment on column last_close_v.net_assets is '마감 순자산';
comment on column last_close_v.units is '마감 좌수';
comment on column last_close_v.nav_per_1000 is '마감 기준가 (1,000좌당)';
comment on column last_close_v.closed_at is '마감 기록 시각';

comment on view abor_positions_v is '마지막 마감 시점의 계좌별·종목별 보유 (ABOR)';
comment on column abor_positions_v.account_id is '계좌 ID';
comment on column abor_positions_v.closed_date is '마감일';
comment on column abor_positions_v.symbol is '종목코드';
comment on column abor_positions_v.qty is '마감 보유수량';
comment on column abor_positions_v.book_cost is '마감 장부금액 (매입금액)';

comment on view unclosed_events_v is '마지막 마감 뒤에 쌓인 이벤트 (IBOR와 ABOR의 차이)';
comment on column unclosed_events_v.account_id is '계좌 ID';
comment on column unclosed_events_v.seq is '이벤트 순번';
comment on column unclosed_events_v.event_type is '이벤트 유형';
comment on column unclosed_events_v.recorded_at is '기록 시각';

-- 5. 새 코드 (ministp.codes.SEED와 같다. tests/db/test_codes.py가 확인) -----------------------
insert into common_codes (group_id, code, name, description, sort_order) values
    ('EVENT_TYPE', 'LateExecutionEvent', '마감 후 체결', '마감한 날의 체결을 열린 날에 정정으로 기록', 7),
    ('EVENT_TYPE', 'DayClosedEvent', '일 마감', '이 날짜까지 장부를 마감', 8),
    ('JOURNAL_KIND', 'LATE_EXECUTION', '마감 후 체결', '마감한 날의 체결을 열린 날 전표로 인식 (정정)', 7);
