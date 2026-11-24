-- title: 주문 결재: 사용자, 결재 권한, 주문 요청, 결재 이력
-- erd: 2.8
-- changes-data: code_groups, common_codes
--
-- ERD v2.7 -> v2.8 (스펙 D9, R-99~101). 새 테이블 네 개, 뷰 두 개, 결재 흐름 트리거 하나, 코드 그룹 세 개와 코드를 더한다.
-- 기존 테이블의 기존 행은 건드리지 않는다. code_groups와 common_codes는 새 행이 늘어나서 위에 적었다.
-- 모든 주문은 요청으로 먼저 남고, 승인된 요청만 `run`이 다시 검증해 증권사로 보낸다.

-- 1. 테이블 ---------------------------------------------------------------------------------
create table app_users (
    user_id    text primary key check (user_id ~ '^[^[:space:]]+$'),
    name       text not null check (name <> ''),
    created_by text not null check (created_by <> ''),
    created_at timestamptz not null default now()
);

create table user_rights (
    right_row_id    bigserial primary key,
    user_id         text not null references app_users (user_id),
    account_id      text references trading_accounts (account_id),
    right_code      text not null check (right_code in ('ORDER_WRITE', 'ORDER_APPROVE', 'RIGHTS_ADMIN')),
    granted         boolean not null,
    max_order_value bigint check (max_order_value > 0),
    self_approve    boolean not null default false,
    effective_from  date not null,
    granted_by      text not null check (granted_by <> ''),
    reason          text,
    created_at      timestamptz not null default now(),
    check (granted or (max_order_value is null and not self_approve)),
    check (right_code = 'ORDER_APPROVE' or (max_order_value is null and not self_approve))
);
create index user_rights_user_idx on user_rights (user_id, right_code);

create table order_requests (
    request_id  text primary key check (request_id ~ '^Q[0-9]{8}-[0-9]{4}$'),
    account_id  text not null references trading_accounts (account_id),
    trade_date  date not null,
    batch_id    text check (batch_id ~ '^[A-Z]{1,3}[0-9]{8}-[0-9]{2,4}$'),
    line_no     integer not null check (line_no > 0),
    symbol      text not null references instruments (symbol),
    side        text not null check (side in ('BUY', 'SELL')),
    qty         bigint not null check (qty > 0),
    limit_price bigint check (limit_price > 0),
    created_by  text not null references app_users (user_id),
    created_at  timestamptz not null default now(),
    constraint order_requests_batch_line_key unique (batch_id, line_no)
);
create index order_requests_account_idx on order_requests (account_id, trade_date);

create table order_approvals (
    id            bigserial primary key,
    request_id    text not null references order_requests (request_id),
    action        text not null check (action in ('REQUEST', 'APPROVE', 'REJECT', 'CANCEL',
                                                  'SEND', 'SEND_FAILED', 'EXPIRE')),
    actor         text references app_users (user_id),
    self_approved boolean not null default false,
    client_id     text references oms_orders (client_id),
    reason        text,
    set_check     jsonb not null default '{}',
    at            timestamptz not null default now(),
    check (actor is not null or action in ('SEND', 'SEND_FAILED', 'EXPIRE')),
    check ((action = 'SEND') = (client_id is not null)),
    check (action not in ('REJECT', 'SEND_FAILED') or coalesce(reason, '') <> ''),
    check (self_approved = false or action = 'APPROVE')
);
create index order_approvals_request_idx on order_approvals (request_id, id);
create unique index order_approvals_client_key on order_approvals (client_id) where client_id is not null;

do $$
declare t text;
begin
    foreach t in array array['app_users', 'user_rights', 'order_requests', 'order_approvals'] loop
        execute format('create trigger %I before update or delete on %I
                        for each row execute function ministp_forbid_change()', t || '_append_only', t);
        execute format('create trigger %I before truncate on %I
                        for each statement execute function ministp_forbid_change()', t || '_no_truncate', t);
    end loop;
end $$;

-- 2. 결재 흐름 (R-100): 요청 → 승인·반려·취소 → 전송·전송 실패, 만료. 그 밖의 순서는 DB가 거부한다.
-- 같은 요청에 두 사람이 동시에 손대도 하나만 통과하도록 요청마다 잠그고 읽는다.
-- 요청과 취소는 작성자만, 셀프 승인 표시는 DB가 작성자와 승인자를 비교해 채운다(프로그램이 넣은 값은 무시).
create or replace function ministp_order_approval_flow() returns trigger language plpgsql as $$
declare
    last_action text;
    writer text;
begin
    perform pg_advisory_xact_lock(hashtext('ministp.order_request:' || new.request_id));
    select created_by into writer from order_requests where request_id = new.request_id;
    select action into last_action from order_approvals
     where request_id = new.request_id order by id desc limit 1;
    if not ((new.action = 'REQUEST' and last_action is null)
         or (new.action in ('APPROVE', 'REJECT') and last_action = 'REQUEST')
         or (new.action = 'CANCEL' and last_action in ('REQUEST', 'APPROVE'))
         or (new.action in ('SEND', 'SEND_FAILED') and last_action = 'APPROVE')
         or (new.action = 'EXPIRE' and last_action in ('REQUEST', 'APPROVE'))) then
        raise exception 'order request %: % after % is not allowed', new.request_id, new.action,
            coalesce(last_action, 'nothing') using errcode = 'check_violation';
    end if;
    if new.action in ('REQUEST', 'CANCEL') and new.actor is distinct from writer then
        raise exception 'order request %: only its writer % may %', new.request_id, writer, new.action
            using errcode = 'check_violation';
    end if;
    new.self_approved := new.action = 'APPROVE' and new.actor = writer;
    return new;
end $$;

create trigger order_approvals_flow before insert on order_approvals
    for each row execute function ministp_order_approval_flow();

-- 3. 뷰 --------------------------------------------------------------------------------
create or replace view user_rights_v as
select r.user_id, u.name as user_name, r.account_id, r.right_code, r.max_order_value, r.self_approve,
       r.effective_from, r.granted_by, r.reason, r.right_row_id
  from (select distinct on (user_id, coalesce(account_id, ''), right_code) *
          from user_rights
         where effective_from <= current_date
         order by user_id, coalesce(account_id, ''), right_code, effective_from desc, right_row_id desc) r
  join app_users u using (user_id)
 where r.granted;

create or replace view order_requests_v as
select q.request_id, q.account_id, q.trade_date, q.batch_id, q.line_no, q.symbol, q.side, q.qty, q.limit_price,
       q.created_by, q.created_at,
       case l.action when 'REQUEST' then 'PENDING' when 'APPROVE' then 'APPROVED' when 'REJECT' then 'REJECTED'
                     when 'CANCEL' then 'CANCELED' when 'SEND' then 'SENT' when 'SEND_FAILED' then 'SEND_FAILED'
                     when 'EXPIRE' then 'EXPIRED' end as status,
       a.actor as approved_by, a.self_approved, a.at as approved_at,
       l.client_id, l.reason as last_reason, l.at as status_at
  from order_requests q
  join lateral (select action, client_id, reason, at from order_approvals o
                 where o.request_id = q.request_id order by id desc limit 1) l on true
  left join lateral (select actor, self_approved, at from order_approvals o
                      where o.request_id = q.request_id and o.action = 'APPROVE' order by id desc limit 1) a on true;

-- 4. 한글 명칭 ---------------------------------------------------------------------------
comment on table app_users is '사용자 (주문을 쓰고 승인하는 사람, 추가만 가능)';
comment on column app_users.user_id is '사용자 ID (지금은 PC 로그인 이름)';
comment on column app_users.name is '이름';
comment on column app_users.created_by is '등록한 사람';
comment on column app_users.created_at is '등록 시각';

comment on table user_rights is '결재 권한 이력 (부여·회수마다 한 행, 수정·삭제 불가)';
comment on column user_rights.right_row_id is '권한 행 번호';
comment on column user_rights.user_id is '사용자 ID';
comment on column user_rights.account_id is '계좌 ID (비어 있으면 모든 계좌)';
comment on column user_rights.right_code is '권한 (ORDER_WRITE 주문 작성 / ORDER_APPROVE 주문 승인 / RIGHTS_ADMIN 권한 관리)';
comment on column user_rights.granted is '부여 여부 (거짓이면 회수 행)';
comment on column user_rights.max_order_value is '1건 승인 한도 (원, 비어 있으면 한도 없음)';
comment on column user_rights.self_approve is '셀프 승인 허용 (자기가 쓴 요청을 승인할 수 있음)';
comment on column user_rights.effective_from is '적용 시작일';
comment on column user_rights.granted_by is '부여·회수한 사람';
comment on column user_rights.reason is '사유';
comment on column user_rights.created_at is '기록 시각';

comment on table order_requests is '주문 요청 (결재를 거쳐 나갈 주문 한 줄, 수정·삭제 불가)';
comment on column order_requests.request_id is '요청 번호 (Q날짜-순번)';
comment on column order_requests.account_id is '계좌 ID';
comment on column order_requests.trade_date is '주문일 (이날 장중에만 나가고 장 마감에 만료)';
comment on column order_requests.batch_id is '묶음 번호 (바스켓·다건 요청, 묶음은 전부 아니면 전무)';
comment on column order_requests.line_no is '줄 번호 (보내는 순서)';
comment on column order_requests.symbol is '종목코드';
comment on column order_requests.side is '매매 구분';
comment on column order_requests.qty is '수량';
comment on column order_requests.limit_price is '지정가 (비어 있으면 시장가)';
comment on column order_requests.created_by is '작성자';
comment on column order_requests.created_at is '작성 시각';

comment on table order_approvals is '결재 이력 (요청·승인·반려·취소·전송·만료마다 한 행, 수정·삭제 불가)';
comment on column order_approvals.id is '이력 번호';
comment on column order_approvals.request_id is '요청 번호';
comment on column order_approvals.action is '결재 행위 (REQUEST 요청 / APPROVE 승인 / REJECT 반려 / CANCEL 취소 / SEND 전송 / SEND_FAILED 전송 실패 / EXPIRE 만료)';
comment on column order_approvals.actor is '처리한 사람 (전송·만료는 비어 있음: run이 처리)';
comment on column order_approvals.self_approved is '셀프 승인 (작성자가 승인, DB가 채움)';
comment on column order_approvals.client_id is '주문 ID (전송한 주문)';
comment on column order_approvals.reason is '사유 (반려·전송 실패는 필수)';
comment on column order_approvals.set_check is '그때의 묶음 검증 결과 (R-98)';
comment on column order_approvals.at is '처리 시각';

comment on view user_rights_v is '현재 결재 권한 (사용자·계좌·권한마다 오늘 적용되는 최근 행, 부여된 것만)';
comment on column user_rights_v.user_id is '사용자 ID';
comment on column user_rights_v.user_name is '이름';
comment on column user_rights_v.account_id is '계좌 ID (비어 있으면 모든 계좌)';
comment on column user_rights_v.right_code is '권한';
comment on column user_rights_v.max_order_value is '1건 승인 한도 (원)';
comment on column user_rights_v.self_approve is '셀프 승인 허용';
comment on column user_rights_v.effective_from is '적용 시작일';
comment on column user_rights_v.granted_by is '부여한 사람';
comment on column user_rights_v.reason is '사유';
comment on column user_rights_v.right_row_id is '권한 행 번호';

comment on view order_requests_v is '주문 요청 현황 (요청마다 한 행, 상태는 결재 이력의 마지막 행)';
comment on column order_requests_v.request_id is '요청 번호';
comment on column order_requests_v.account_id is '계좌 ID';
comment on column order_requests_v.trade_date is '주문일';
comment on column order_requests_v.batch_id is '묶음 번호';
comment on column order_requests_v.line_no is '줄 번호';
comment on column order_requests_v.symbol is '종목코드';
comment on column order_requests_v.side is '매매 구분';
comment on column order_requests_v.qty is '수량';
comment on column order_requests_v.limit_price is '지정가';
comment on column order_requests_v.created_by is '작성자';
comment on column order_requests_v.created_at is '작성 시각';
comment on column order_requests_v.status is '상태 (PENDING 결재 대기 / APPROVED 승인·전송 대기 / REJECTED 반려 / CANCELED 취소 / SENT 전송 / SEND_FAILED 전송 실패 / EXPIRED 만료)';
comment on column order_requests_v.approved_by is '승인자';
comment on column order_requests_v.self_approved is '셀프 승인';
comment on column order_requests_v.approved_at is '승인 시각';
comment on column order_requests_v.client_id is '주문 ID';
comment on column order_requests_v.last_reason is '마지막 사유';
comment on column order_requests_v.status_at is '상태 시각';

-- 5. 새 코드 그룹과 코드 (ministp.codes.SEED와 같다. tests/db/test_codes.py가 확인) -----------
insert into code_groups (group_id, name, description, used_by, fixed) values
    ('USER_RIGHT', '결재 권한', '사용자가 주문에 대해 할 수 있는 일 (R-99).', 'user_rights.right_code', true),
    ('APPROVAL_ACTION', '결재 행위', '주문 요청에 일어난 일. 순서는 DB가 지켜요 (R-100).', 'order_approvals.action', true),
    ('REQUEST_STATUS', '주문 요청 상태', '결재 이력의 마지막 행으로 정해지는 요청의 상태.', 'order_requests_v.status', true);
insert into common_codes (group_id, code, name, description, sort_order) values
    ('AUDIT_ACTION', 'USER_ADD', '사용자 등록', null, 25),
    ('AUDIT_ACTION', 'RIGHT_GRANT', '권한 부여', '결재 권한을 주거나 한도를 바꿈', 26),
    ('AUDIT_ACTION', 'RIGHT_REVOKE', '권한 회수', null, 27),
    ('AUDIT_ACTION', 'ORDER_REQUEST', '주문 결재 요청', '묶음 검증(R-98)을 통과한 요청만 남아요', 28),
    ('AUDIT_ACTION', 'ORDER_APPROVE', '주문 승인', '셀프 승인 포함', 29),
    ('AUDIT_ACTION', 'ORDER_REJECT', '주문 반려', null, 30),
    ('AUDIT_ACTION', 'ORDER_REQUEST_CANCEL', '주문 요청 취소', '작성자가 전송 전에 거둠', 31),
    ('USER_RIGHT', 'ORDER_WRITE', '주문 작성', '주문 요청을 쓰고 결재를 요청함', 1),
    ('USER_RIGHT', 'ORDER_APPROVE', '주문 승인', '요청을 승인·반려함. 1건 한도와 셀프 승인 허용을 함께 둠', 2),
    ('USER_RIGHT', 'RIGHTS_ADMIN', '권한 관리', '사용자를 등록하고 권한을 주거나 거둠', 3),
    ('APPROVAL_ACTION', 'REQUEST', '결재 요청', '작성자가 요청을 올림', 1),
    ('APPROVAL_ACTION', 'APPROVE', '승인', '승인자가 승인함. 작성자 본인이면 셀프 승인', 2),
    ('APPROVAL_ACTION', 'REJECT', '반려', '승인자가 사유와 함께 돌려보냄', 3),
    ('APPROVAL_ACTION', 'CANCEL', '요청 취소', '작성자가 전송 전에 거둠', 4),
    ('APPROVAL_ACTION', 'SEND', '전송', 'run이 다시 검증하고 주문으로 보냄', 5),
    ('APPROVAL_ACTION', 'SEND_FAILED', '전송 실패', '보내기 직전 검증에 걸리거나 주문 경로가 거부함', 6),
    ('APPROVAL_ACTION', 'EXPIRE', '만료', '주문일 장 마감까지 나가지 않음', 7),
    ('REQUEST_STATUS', 'PENDING', '결재 대기', null, 1),
    ('REQUEST_STATUS', 'APPROVED', '승인', '전송 대기', 2),
    ('REQUEST_STATUS', 'REJECTED', '반려', null, 3),
    ('REQUEST_STATUS', 'CANCELED', '취소', null, 4),
    ('REQUEST_STATUS', 'SENT', '전송', '주문 ID가 붙음', 5),
    ('REQUEST_STATUS', 'SEND_FAILED', '전송 실패', null, 6),
    ('REQUEST_STATUS', 'EXPIRED', '만료', null, 7);
