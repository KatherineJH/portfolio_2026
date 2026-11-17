// 선택한 요청의 처리안, 수량 통제, 상태 흐름, Agent 실행 기록, 동작을 한 화면에 모은다.

import { useState } from 'react'
import type { PendingProposal, TraceRow } from '../api'
import { CapacityBar } from './CapacityBar'
import { LifecycleSteps } from './LifecycleSteps'
import { approvalForecast, ownHeldQty, shortDigest, type Operator, type QueueState } from './model'

export type ActionKind = 'approve' | 'reject' | 'execute' | 'release' | 're_review'

type Props = {
  state: QueueState
  item: PendingProposal
  operator: Operator
  busy: boolean
  trace: TraceRow[] | undefined
  onAction: (kind: ActionKind, reason?: string) => void
}

const REASON_PROMPT: Record<'release' | 're_review', { title: string; placeholder: string; confirm: string }> = {
  release: {
    title: '예약 해제 사유',
    placeholder: '예: 임직원이 교체 요청을 취소함',
    confirm: '예약 해제 · 승인 철회',
  },
  re_review: {
    title: '재검토 사유',
    placeholder: '예: 재고가 입고되어 다시 승인 대기로 보냄',
    confirm: '승인 대기로 보내기',
  },
}

export function ProposalDetail({ state, item, operator, busy, trace, onAction }: Props) {
  const [reasonFor, setReasonFor] = useState<'release' | 're_review' | null>(null)
  const [reason, setReason] = useState('')

  const qty = item.payload.qty
  const own = ownHeldQty(item)
  const preview = state === 'ready_to_execute' ? 0 : qty
  const forecast = approvalForecast(item)
  const onHand = item.on_hand_qty ?? 0
  const approveWhen = state === 'needs_review' ? '재검토 후 재승인하면' : '승인하면'
  const refusedThen = state === 'needs_review'
    ? '재승인해도 거절됩니다.'
    : '승인이 거절되고 재검토로 이동합니다.'

  const totals = (trace ?? []).reduce(
    (acc, row) => ({
      latency: acc.latency + (row.latency_ms ?? 0),
      tokens: acc.tokens + (row.prompt_tokens ?? 0) + (row.completion_tokens ?? 0),
      cost: acc.cost + Number(row.cost_usd ?? 0),
    }),
    { latency: 0, tokens: 0, cost: 0 },
  )

  function submitReason() {
    const text = reason.trim()
    if (!reasonFor || !text) return
    onAction(reasonFor, text)
    setReasonFor(null)
    setReason('')
  }

  const needsApproval = !operator.canApprove

  return (
    <article className="detail-panel">
      <header className="detail-head">
        <div>
          <span className="detail-kicker">Request #{item.request_id} · Proposal #{item.proposal_id}</span>
          <h2>{item.requester} · {item.asset_name}</h2>
          <span className="detail-sub">{item.asset_code} · {new Date(item.created_at).toLocaleString('ko-KR')}</span>
        </div>
        <div className="detail-badges">
          <span className={`state-chip ${state}`}>
            {state === 'awaiting_approval' ? '승인 대기' : state === 'ready_to_execute' ? '실행 대기' : '재검토 필요'}
          </span>
          {item.reservation_status && (
            <span className={`reservation-chip ${item.reservation_status}`}>예약 {item.reservation_status}</span>
          )}
        </div>
      </header>

      <section className="detail-block">
        <h3><span className="axis-tag">Agent</span>AI 처리안</h3>
        <dl className="proposal-facts">
          <div><dt>처리 내용</dt><dd>교체 <b>{qty}</b>개</dd></div>
          <div><dt>처리안 버전</dt><dd className="mono">v{item.version}</dd></div>
          <div>
            <dt>payload digest</dt>
            <dd className="mono" title={item.payload_digest}>{shortDigest(item.payload_digest)}</dd>
          </div>
          <div>
            <dt>근거 규정 (RAG)</dt>
            <dd>
              {item.policy_refs.length === 0 ? (
                <span className="muted">인용 보류: 검색 후보에 처리 주제 규정이 없었습니다</span>
              ) : (
                item.policy_refs.map((ref) => (
                  <span className="policy-chip" key={`${ref.policy_key}-${ref.version}`}>
                    {ref.policy_key} · v{ref.version}
                  </span>
                ))
              )}
            </dd>
          </div>
        </dl>
        <p className="binding-note">
          승인과 실행은 이 버전과 digest에 결속됩니다. 처리안이 바뀌면 이전 승인으로는 실행할 수 없습니다.
        </p>
      </section>

      <section className="detail-block">
        <h3><span className="axis-tag control">Control</span>수량 통제</h3>
        <div className="capacity-grid">
          <CapacityBar
            label="교체 재고"
            totalLabel="보유"
            total={onHand}
            heldByOthers={Math.max(item.held_stock_qty - own, 0)}
            heldByThis={own}
            preview={preview}
          />
          <CapacityBar
            label="지급 항목 잔여 수량"
            totalLabel="미처리"
            total={item.remaining_item_qty}
            heldByOthers={Math.max(item.held_item_qty - own, 0)}
            heldByThis={own}
            preview={preview}
          />
        </div>
        <p className={`forecast ${state === 'ready_to_execute' || forecast === 'ok' ? 'ok' : 'warn'}`}>
          {state === 'ready_to_execute'
            ? `예약 ${own}개를 보유 중입니다. 실행은 새 재고와 경쟁하지 않고 이 예약을 소비합니다.`
            : forecast === 'ok'
              ? `${approveWhen} 재고와 지급 수량을 ${qty}개씩 함께 예약합니다.`
              : forecast === 'stock'
                ? `예상: 예약 가능한 재고가 부족해 ${refusedThen}`
                : `예상: 지급 항목의 예약 가능 수량이 부족해 ${refusedThen}`}
          <small>최종 판단은 서버가 행 잠금 안에서 다시 계산합니다.</small>
        </p>
      </section>

      <section className="detail-block">
        <h3><span className="axis-tag control">Control</span>처리 흐름</h3>
        <LifecycleSteps state={state} item={item} />
      </section>

      <section className="detail-block">
        <h3><span className="axis-tag">Agent</span>Agent 실행 기록</h3>
        {trace === undefined ? (
          <p className="muted">불러오는 중</p>
        ) : trace.length === 0 ? (
          <p className="muted">이 요청의 실행 기록이 없습니다.</p>
        ) : (
          <>
            <ol className="trace-nodes">
              {trace.map((row, index) => (
                <li key={`${row.node_name}-${index}`} className={row.outcome === 'failed' ? 'failed' : ''}>
                  <strong>{row.node_name}</strong>
                  <span>{row.latency_ms ?? 0}ms</span>
                  <span>{row.model ?? 'server'}</span>
                  <span>
                    {row.prompt_tokens == null ? '토큰 없음' : `${row.prompt_tokens + (row.completion_tokens ?? 0)} tokens`}
                  </span>
                </li>
              ))}
            </ol>
            <p className="trace-total">
              합계 {totals.latency.toLocaleString()}ms · {totals.tokens.toLocaleString()} tokens · ${totals.cost.toFixed(6)}
            </p>
          </>
        )}
      </section>

      <footer className="detail-actions">
        {reasonFor ? (
          <div className="reason-form">
            <label htmlFor="action-reason">{REASON_PROMPT[reasonFor].title}</label>
            <textarea
              id="action-reason"
              rows={2}
              maxLength={300}
              value={reason}
              placeholder={REASON_PROMPT[reasonFor].placeholder}
              onChange={(event) => setReason(event.target.value)}
            />
            <div className="button-row">
              <button
                type="button"
                className={reasonFor === 'release' ? 'btn btn-danger' : 'btn btn-primary'}
                disabled={busy || !reason.trim()}
                onClick={submitReason}
              >
                {REASON_PROMPT[reasonFor].confirm}
              </button>
              <button type="button" className="btn btn-ghost" onClick={() => setReasonFor(null)}>
                취소
              </button>
            </div>
            <small>사유는 감사 기록에 남습니다.</small>
          </div>
        ) : (
          <div className="button-row">
            {state === 'awaiting_approval' && (
              <>
                <button type="button" className="btn btn-primary" disabled={busy || needsApproval} onClick={() => onAction('approve')}>
                  승인 · 예약
                </button>
                <button type="button" className="btn btn-danger-outline" disabled={busy || needsApproval} onClick={() => onAction('reject')}>
                  거절
                </button>
              </>
            )}
            {state === 'ready_to_execute' && (
              <>
                <button type="button" className="btn btn-primary" disabled={busy} onClick={() => onAction('execute')}>
                  실행 · 예약 소비
                </button>
                <button type="button" className="btn btn-danger-outline" disabled={busy || needsApproval} onClick={() => setReasonFor('release')}>
                  예약 해제
                </button>
              </>
            )}
            {state === 'needs_review' && (
              <button type="button" className="btn btn-primary" disabled={busy || needsApproval} onClick={() => setReasonFor('re_review')}>
                재검토 · 승인 대기로
              </button>
            )}
          </div>
        )}
        {needsApproval && (
          <p className="permission-note">
            <b>승인 권한 필요</b>
            {state === 'ready_to_execute'
              ? ' 실행은 IT 담당자 누구나 할 수 있지만, 예약 해제는 승인 권한이 있는 담당자만 할 수 있습니다.'
              : ' 승인·거절·해제·재검토는 승인 권한이 있는 담당자만 할 수 있습니다. 서버도 같은 규칙으로 확인합니다.'}
          </p>
        )}
      </footer>
    </article>
  )
}
