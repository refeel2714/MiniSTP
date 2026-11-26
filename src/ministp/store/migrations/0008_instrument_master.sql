-- title: 종목 마스터 이력: 시장 전 종목의 속성 변경 이력과 적재 기록
-- erd: 2.7
-- changes-data: code_groups, common_codes
--
-- ERD v2.6 -> v2.7 (R-94, R-95). 새 테이블 두 개, 뷰 두 개, 코드 그룹 두 개와 코드를 더한다.
-- 기존 테이블의 기존 행은 건드리지 않는다. code_groups와 common_codes는 새 행이 늘어나서 위에 적었다.
-- 등록 종목(instruments)은 그대로 원장이 쓰는 종목이다. 이력은 시장 전체를 따로 담고, 대조 뷰가 둘을 맞춰 본다.

-- 1. 적재 기록: master load 한 번에 한 행 --------------------------------------------------
create table master_loads (
    load_id       bigserial primary key,
    base_date     date not null,
    fetched_at    timestamptz not null,
    kospi_sha256  text not null check (kospi_sha256 ~ '^[0-9a-f]{64}$'),
    kosdaq_sha256 text not null check (kosdaq_sha256 ~ '^[0-9a-f]{64}$'),
    rows_total    integer not null check (rows_total >= 0),
    rows_new      integer not null check (rows_new >= 0),
    rows_changed  integer not null check (rows_changed >= 0),
    rows_delisted integer not null check (rows_delisted >= 0),
    loaded_at     timestamptz not null default now(),
    constraint master_loads_base_date_key unique (base_date),
    constraint master_loads_files_key unique (kospi_sha256, kosdaq_sha256)
);

-- 2. 종목 마스터 이력: 속성이 바뀐 날에만 한 행 (가격은 담지 않는다) ------------------------------
create table instrument_master (
    master_id       bigserial primary key,
    symbol          text not null,
    valid_from      date not null,
    load_id         bigint not null references master_loads (load_id),
    listed          boolean not null,
    isin            text not null check (isin ~ '^[A-Z0-9]{12}$'),
    name            text not null check (name <> ''),
    market          text not null check (market in ('KOSPI', 'KOSDAQ')),
    sec_group       text not null check (sec_group ~ '^[A-Z]{2}$'),
    lot             integer not null check (lot >= 0),
    par_value       bigint not null check (par_value >= 0),
    listed_on       date,
    listed_shares_k bigint not null check (listed_shares_k >= 0),
    preferred       text not null,
    spac            boolean not null,
    halted          boolean not null,
    liquidation     boolean not null,
    managed         boolean not null,
    warning         text not null,
    constraint instrument_master_symbol_key unique (symbol, valid_from)
);
create index instrument_master_load_idx on instrument_master (load_id);

do $$
declare t text;
begin
    foreach t in array array['master_loads', 'instrument_master'] loop
        execute format('create trigger %I before update or delete on %I
                        for each row execute function ministp_forbid_change()', t || '_append_only', t);
        execute format('create trigger %I before truncate on %I
                        for each statement execute function ministp_forbid_change()', t || '_no_truncate', t);
    end loop;
end $$;

-- 3. 뷰 --------------------------------------------------------------------------------
create or replace view instrument_master_current_v as
select distinct on (m.symbol) m.symbol, m.listed, m.name, m.market, m.sec_group, m.isin, m.lot, m.par_value,
       m.listed_on, m.listed_shares_k, m.preferred, m.spac, m.halted, m.liquidation, m.managed, m.warning,
       m.valid_from, m.load_id
  from instrument_master m
 order by m.symbol, m.valid_from desc;

create or replace view instrument_master_match_v as
select i.symbol, i.name, i.market, i.asset_type,
       c.name as master_name, c.market as master_market, c.sec_group as master_group, c.listed,
       c.halted, c.liquidation, c.managed, c.warning, c.valid_from,
       case when c.symbol is null then 'MISSING'
            when not c.listed then 'DELISTED'
            when c.market <> i.market
              or c.sec_group <> case i.asset_type when 'STOCK' then 'ST' when 'ETF' then 'EF' end then 'DIFFERENT'
            when replace(c.name, ' ', '') <> replace(i.name, ' ', '') then 'RENAMED'
            else 'MATCH' end as status
  from instruments i left join instrument_master_current_v c using (symbol);

-- 4. 한글 명칭 ---------------------------------------------------------------------------
comment on table master_loads is '종목 마스터 적재 기록 (KIS 마스터 파일을 DB에 넣은 날마다 한 행, 수정·삭제 불가)';
comment on column master_loads.load_id is '적재 번호';
comment on column master_loads.base_date is '기준일 (파일을 받은 날)';
comment on column master_loads.fetched_at is '파일을 받은 시각';
comment on column master_loads.kospi_sha256 is '코스피 파일 해시 (SHA-256)';
comment on column master_loads.kosdaq_sha256 is '코스닥 파일 해시 (SHA-256)';
comment on column master_loads.rows_total is '종목 수';
comment on column master_loads.rows_new is '신규 (처음 나타난 종목 수)';
comment on column master_loads.rows_changed is '변경 (속성이 바뀐 종목 수)';
comment on column master_loads.rows_delisted is '빠짐 (파일에서 빠진 종목 수)';
comment on column master_loads.loaded_at is '적재 시각';

comment on table instrument_master is '종목 마스터 이력 (시장 전 종목, 속성이 바뀐 날에만 한 행, 수정·삭제 불가)';
comment on column instrument_master.master_id is '이력 번호';
comment on column instrument_master.symbol is '단축코드';
comment on column instrument_master.valid_from is '적용 시작일 (이 속성이 처음 보인 기준일)';
comment on column instrument_master.load_id is '적재 번호';
comment on column instrument_master.listed is '상장 여부 (파일에서 빠지면 거짓인 행이 생김)';
comment on column instrument_master.isin is '표준코드 (ISIN)';
comment on column instrument_master.name is '종목명';
comment on column instrument_master.market is '시장 (KOSPI / KOSDAQ)';
comment on column instrument_master.sec_group is '증권그룹 구분 (ST 주권, EF ETF 등)';
comment on column instrument_master.lot is '매매수량단위';
comment on column instrument_master.par_value is '액면가 (원)';
comment on column instrument_master.listed_on is '상장일';
comment on column instrument_master.listed_shares_k is '상장주수 (천 주)';
comment on column instrument_master.preferred is '우선주 구분 (0 보통주)';
comment on column instrument_master.spac is '스팩 여부';
comment on column instrument_master.halted is '거래정지 여부';
comment on column instrument_master.liquidation is '정리매매 여부';
comment on column instrument_master.managed is '관리종목 여부';
comment on column instrument_master.warning is '시장경고 구분 (00 없음, 01 투자주의, 02 투자경고, 03 투자위험)';

comment on view instrument_master_current_v is '종목 마스터 현재 (종목마다 가장 최근 이력 행)';
comment on column instrument_master_current_v.symbol is '단축코드';
comment on column instrument_master_current_v.listed is '상장 여부';
comment on column instrument_master_current_v.name is '종목명';
comment on column instrument_master_current_v.market is '시장';
comment on column instrument_master_current_v.sec_group is '증권그룹 구분';
comment on column instrument_master_current_v.isin is '표준코드 (ISIN)';
comment on column instrument_master_current_v.lot is '매매수량단위';
comment on column instrument_master_current_v.par_value is '액면가 (원)';
comment on column instrument_master_current_v.listed_on is '상장일';
comment on column instrument_master_current_v.listed_shares_k is '상장주수 (천 주)';
comment on column instrument_master_current_v.preferred is '우선주 구분';
comment on column instrument_master_current_v.spac is '스팩 여부';
comment on column instrument_master_current_v.halted is '거래정지 여부';
comment on column instrument_master_current_v.liquidation is '정리매매 여부';
comment on column instrument_master_current_v.managed is '관리종목 여부';
comment on column instrument_master_current_v.warning is '시장경고 구분';
comment on column instrument_master_current_v.valid_from is '적용 시작일';
comment on column instrument_master_current_v.load_id is '적재 번호';

comment on view instrument_master_match_v is '등록 종목과 종목 마스터 대조 (원장이 쓰는 종목마다 한 행)';
comment on column instrument_master_match_v.symbol is '종목코드';
comment on column instrument_master_match_v.name is '등록 종목명';
comment on column instrument_master_match_v.market is '등록 시장';
comment on column instrument_master_match_v.asset_type is '등록 자산 유형';
comment on column instrument_master_match_v.master_name is '마스터 종목명';
comment on column instrument_master_match_v.master_market is '마스터 시장';
comment on column instrument_master_match_v.master_group is '마스터 증권그룹 구분';
comment on column instrument_master_match_v.listed is '마스터 상장 여부';
comment on column instrument_master_match_v.halted is '거래정지 여부';
comment on column instrument_master_match_v.liquidation is '정리매매 여부';
comment on column instrument_master_match_v.managed is '관리종목 여부';
comment on column instrument_master_match_v.warning is '시장경고 구분';
comment on column instrument_master_match_v.valid_from is '마스터 적용 시작일';
comment on column instrument_master_match_v.status is '대조 결과 (MATCH 일치 / RENAMED 이름만 다름 / DIFFERENT 시장·구분 다름 / DELISTED 상장폐지 / MISSING 마스터에 없음)';

-- 5. 새 코드 그룹과 코드 (ministp.codes.SEED와 같다. tests/db/test_codes.py가 확인) -----------
insert into code_groups (group_id, name, description, used_by, fixed) values
    ('SECURITY_GROUP', '증권그룹 구분', 'KIS 마스터 파일이 종목마다 붙이는 증권 종류.', 'instrument_master.sec_group', true),
    ('MASTER_MATCH', '종목 마스터 대조 결과', '등록 종목이 시장 마스터와 같은지 (R-95).', 'instrument_master_match_v.status', true);
insert into common_codes (group_id, code, name, description, sort_order) values
    ('AUDIT_ACTION', 'MASTER_LOAD', '종목 마스터 적재', '받은 마스터 파일에서 바뀐 종목만 이력으로 넣음', 24),
    ('SECURITY_GROUP', 'ST', '주권', null, 1),
    ('SECURITY_GROUP', 'EF', 'ETF', null, 2),
    ('SECURITY_GROUP', 'EN', 'ETN', null, 3),
    ('SECURITY_GROUP', 'RT', '리츠', null, 4),
    ('SECURITY_GROUP', 'MF', '증권투자회사', null, 5),
    ('SECURITY_GROUP', 'SC', '선박투자회사', null, 6),
    ('SECURITY_GROUP', 'IF', '인프라투융자회사', null, 7),
    ('SECURITY_GROUP', 'DR', '주식예탁증서', null, 8),
    ('SECURITY_GROUP', 'BC', '수익증권', null, 9),
    ('SECURITY_GROUP', 'FS', '외국주권', null, 10),
    ('SECURITY_GROUP', 'FE', '해외ETF', null, 11),
    ('SECURITY_GROUP', 'SW', '신주인수권증권', null, 12),
    ('SECURITY_GROUP', 'SR', '신주인수권증서', null, 13),
    ('SECURITY_GROUP', 'EW', 'ELW', null, 14),
    ('SECURITY_GROUP', 'PF', '상장 공모펀드', null, 15),
    ('MASTER_MATCH', 'MATCH', '일치', '시장, 구분, 이름이 같음', 1),
    ('MASTER_MATCH', 'RENAMED', '이름만 다름', '띄어쓰기 말고 이름이 다름. 종목명 변경일 수 있음', 2),
    ('MASTER_MATCH', 'DIFFERENT', '시장·구분 다름', '등록할 때 시장이나 자산 유형을 잘못 넣었을 수 있음', 3),
    ('MASTER_MATCH', 'DELISTED', '상장폐지', '마스터 파일에서 빠진 종목', 4),
    ('MASTER_MATCH', 'MISSING', '마스터에 없음', '적재한 마스터에 한 번도 나온 적 없는 코드', 5);
