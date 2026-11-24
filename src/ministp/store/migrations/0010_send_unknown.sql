-- title: 주문 결과 미확인 처리: 요청 상태에 증권사 접수 결과 반영
-- erd: 2.10
-- changes-data: common_codes
--
-- ERD v2.9 -> v2.10 (스펙 R-103). 테이블은 늘지 않는다. 주문 요청 뷰 두 개가 증권사 접수 결과(oms_order_acks)를 함께 읽어서,
-- 보냈지만 답이 없는 줄(결과 미확인)과 증권사가 거부한 줄을 구분한다. 요청 상태 코드 하나(SEND_UNKNOWN)와
-- 감사 행위 코드 하나(ORDER_CONFIRM)를 더한다. 기존 행은 건드리지 않는다.

create or replace view order_requests_v as
select q.trade_date, q.request_no, q.account_id, q.order_type, q.created_by, q.created_at,
       n.lines as line_count, n.amount,
       case when l.action = 'APPROVE' and s.unknown > 0 then 'SEND_UNKNOWN'
            when l.action = 'APPROVE' and s.done = n.lines then case when s.accepted > 0 then 'SENT' else 'SEND_FAILED' end
            else case l.action when 'REQUEST' then 'PENDING' when 'APPROVE' then 'APPROVED' when 'REJECT' then 'REJECTED'
                               when 'CANCEL' then 'CANCELED' when 'EXPIRE' then 'EXPIRED' end end as status,
       a.actor as approved_by, a.self_approved, a.at as approved_at,
       coalesce(s.sent, 0) as sent_lines, coalesce(s.failed, 0) as failed_lines,
       case when l.action = 'APPROVE' and (s.failed > 0 or s.rejected > 0) then s.reason else l.reason end as last_reason,
       greatest(l.at, s.at) as status_at,
       coalesce(s.accepted, 0) as accepted_lines, coalesce(s.rejected, 0) as rejected_lines,
       coalesce(s.unknown, 0) as unknown_lines
  from order_requests q
  join lateral (select count(*) as lines, sum(qty * limit_price) as amount from order_request_lines r
                 where r.trade_date = q.trade_date and r.request_no = q.request_no) n on true
  join lateral (select action, reason, at from order_approvals o
                 where o.trade_date = q.trade_date and o.request_no = q.request_no order by id desc limit 1) l on true
  left join lateral (select actor, self_approved, at from order_approvals o
                      where o.trade_date = q.trade_date and o.request_no = q.request_no and o.action = 'APPROVE'
                      order by id desc limit 1) a on true
  left join lateral (select count(*) as done, count(*) filter (where x.outcome = 'SENT') as sent,
                            count(*) filter (where x.outcome = 'FAILED') as failed,
                            count(*) filter (where k.outcome = 'ACCEPTED') as accepted,
                            count(*) filter (where k.outcome = 'REJECTED') as rejected,
                            count(*) filter (where x.outcome = 'SENT' and k.client_id is null) as unknown,
                            greatest(max(x.at), max(k.created_at)) as at,
                            (array_agg(coalesce(x.reason, k.detail) order by x.id desc)
                               filter (where x.outcome = 'FAILED' or k.outcome = 'REJECTED'))[1] as reason
                       from order_sends x left join oms_order_acks k on k.client_id = x.client_id
                      where x.trade_date = q.trade_date and x.request_no = q.request_no) s on true;

create or replace view order_request_lines_v as
select r.trade_date, r.request_no, r.line_no, q.account_id, q.order_type, r.symbol, r.side, r.qty, r.limit_price,
       r.qty * r.limit_price as amount, s.outcome as send_outcome, s.client_id, s.reason as send_reason, s.at as sent_at,
       k.outcome as ack_outcome, k.order_no, k.detail as ack_detail,
       (s.outcome = 'SENT' and k.client_id is null) as awaiting_ack
  from order_request_lines r
  join order_requests q using (trade_date, request_no)
  left join order_sends s using (trade_date, request_no, line_no)
  left join oms_order_acks k on k.client_id = s.client_id;

comment on column order_requests_v.status is '상태 (PENDING 결재 대기 / APPROVED 승인·전송 대기 / REJECTED 반려 / CANCELED 취소 / SENT 전송 / SEND_FAILED 전송 실패 / SEND_UNKNOWN 결과 미확인 / EXPIRED 만료)';
comment on column order_requests_v.accepted_lines is '증권사가 접수한 줄 수';
comment on column order_requests_v.rejected_lines is '증권사가 거부한 줄 수';
comment on column order_requests_v.unknown_lines is '보냈지만 접수 결과를 모르는 줄 수 (R-103)';
comment on column order_request_lines_v.ack_outcome is '증권사 접수 결과 (ACCEPTED 접수 / REJECTED 거부, 비어 있으면 아직 모름)';
comment on column order_request_lines_v.order_no is '증권사 주문번호';
comment on column order_request_lines_v.ack_detail is '접수 결과 내용 (거부 사유, 조회로 찾음 등)';
comment on column order_request_lines_v.awaiting_ack is '보냈지만 접수 결과를 모름 (R-103)';

insert into common_codes (group_id, code, name, description, sort_order) values
    ('REQUEST_STATUS', 'SEND_UNKNOWN', '결과 미확인', '보낸 줄 가운데 증권사 답이 없는 줄이 있음. 찾을 때까지 다음 전송을 멈춰요', 8),
    ('AUDIT_ACTION', 'ORDER_CONFIRM', '주문 결과 확인', '답이 없던 주문을 사람이 증권사 주문내역과 맞춰 확인함', 32);
