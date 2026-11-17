// 승인 관리: 운영 요약, 대기열, 선택한 요청의 상세와 동작.

import { useCallback, useEffect, useState } from 'react'
import {
  ApiError,
  decide,
  execute,
  getTrace,
  listPending,
  releaseProposal,
  reReviewProposal,
  type PendingProposal,
  type TraceRow,
} from '../api'
import { ProposalDetail, type ActionKind } from './ProposalDetail'
import { approvalForecast, ownHeldQty, QUEUE_LABEL, type Operator, type QueueState } from './model'

const QUEUES: QueueState[] = ['awaiting_approval', 'ready_to_execute', 'needs_review']

const CONTROLS = [
  '처리안 버전·digest 결속',
  '승인 시 수량 예약',
  '잠금 순서 고정',
  '실행 키 멱등성',
  '권한은 서버가 DB에서 확인',
]

type Banner =
  | { tone: 'success'; title: string; detail: string }
  | { tone: 'refused'; title: string; detail: string; code: string | null }

type Queues = Record<QueueState, PendingProposal[]>

const EMPTY: Queues = { awaiting_approval: [], ready_to_execute: [], needs_review: [] }

export function ApprovalsView({ operator }: { operator: Operator }) {
  const [queues, setQueues] = useState<Queues>(EMPTY)
  const [tab, setTab] = useState<QueueState>('awaiting_approval')
  const [selected, setSelected] = useState<number | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [banner, setBanner] = useState<Banner | null>(null)
  const [traces, setTraces] = useState<Record<number, TraceRow[]>>({})

  const refresh = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const results = await Promise.all(QUEUES.map((state) => listPending(state, operator.id)))
      setQueues({
        awaiting_approval: results[0].items,
        ready_to_execute: results[1].items,
        needs_review: results[2].items,
      })
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [operator.id])

  useEffect(() => {
    const timer = window.setTimeout(() => void refresh(), 0)
    return () => window.clearTimeout(timer)
  }, [refresh])

  const items = queues[tab]
  const current = items.find((item) => item.proposal_id === selected) ?? items[0] ?? null
  const currentRequest = current?.request_id ?? null

  useEffect(() => {
    if (currentRequest === null || traces[currentRequest]) return
    let cancelled = false
    getTrace(currentRequest, operator.id)
      .then((data) => {
        if (!cancelled) setTraces((prev) => ({ ...prev, [currentRequest]: data.items }))
      })
      .catch(() => {
        if (!cancelled) setTraces((prev) => ({ ...prev, [currentRequest]: [] }))
      })
    return () => {
      cancelled = true
    }
  }, [currentRequest, operator.id, traces])

  const heldRows = queues.ready_to_execute.filter((item) => item.reservation_status === 'held')
  const heldQty = heldRows.reduce((sum, item) => sum + ownHeldQty(item), 0)

  async function onAction(kind: ActionKind, reason?: string) {
    if (!current || busy) return
    setBusy(true)
    setBanner(null)
    try {
      if (kind === 'approve' || kind === 'reject') {
        const result = await decide(current.proposal_id, kind, operator.id)
        setBanner(kind === 'approve'
          ? {
              tone: 'success',
              title: '승인 · 예약 완료',
              detail: `예약 #${result.reservation_id ?? '-'} 생성. 재고와 지급 수량 ${current.payload.qty}개를 확보했습니다.`,
            }
          : { tone: 'success', title: '거절 완료', detail: '예약은 만들지 않았고 거절 결정은 승인 기록에 남았습니다.' })
      } else if (kind === 'execute') {
        const result = await execute(current.request_id, current.version, operator.id)
        setBanner({
          tone: 'success',
          title: '실행 완료',
          detail: `실행 키 ${result.execution_key}. 예약을 소비하고 재고·지급 수량을 한 트랜잭션에서 반영했습니다.`,
        })
      } else if (kind === 'release') {
        const result = await releaseProposal(current.proposal_id, reason ?? '', operator.id)
        setBanner({
          tone: 'success',
          title: result.outcome === 'already_released' ? '이미 해제된 예약' : '예약 해제 완료',
          detail: '연결된 승인을 철회했고 요청은 재검토로 이동했습니다.',
        })
      } else {
        const result = await reReviewProposal(current.proposal_id, reason ?? '', operator.id)
        setBanner({
          tone: 'success',
          title: result.outcome === 'already_awaiting_approval' ? '이미 승인 대기' : '재검토 완료',
          detail: '같은 처리안으로 승인 대기에 돌아갔습니다. 재승인하면 새 예약을 만듭니다.',
        })
      }
    } catch (e) {
      setBanner({
        tone: 'refused',
        title: '서버가 요청을 거절했습니다',
        detail: e instanceof Error ? e.message : String(e),
        code: e instanceof ApiError ? e.reasonCode ?? `HTTP ${e.status}` : null,
      })
    } finally {
      setBusy(false)
      await refresh()
    }
  }

  return (
    <section className="approvals-view">
      <div className="view-heading">
        <div>
          <span className="eyebrow">Operator workspace · Approval control</span>
          <h1>승인 관리</h1>
          <p>AI는 처리안만 제안합니다. 승인·예약·실행은 서버 규칙과 DB 제약이 통제합니다.</p>
        </div>
        <button type="button" className="btn btn-secondary" onClick={() => void refresh()} disabled={loading}>
          {loading ? '불러오는 중' : '새로고침'}
        </button>
      </div>

      <div className="ops-summary" aria-label="운영 요약">
        {QUEUES.map((state) => (
          <button
            type="button"
            key={state}
            className={tab === state ? `ops-tile ${state} on` : `ops-tile ${state}`}
            onClick={() => {
              setTab(state)
              setSelected(null)
            }}
          >
            <span>{QUEUE_LABEL[state]}</span>
            <strong>{queues[state].length}</strong>
            <small>
              {state === 'awaiting_approval' && '승인하면 수량을 예약'}
              {state === 'ready_to_execute' && '예약을 보유한 요청'}
              {state === 'needs_review' && '수량 부족 거절 · 해제'}
            </small>
          </button>
        ))}
        <div className="ops-tile reserved">
          <span>예약으로 확보된 수량</span>
          <strong>{heldQty}</strong>
          <small>held 예약 {heldRows.length}건 · 재고와 지급 수량 동시 점유</small>
        </div>
      </div>

      <ul className="control-strip" aria-label="실행 안전장치">
        {CONTROLS.map((control) => <li key={control}>{control}</li>)}
      </ul>

      {error && <p className="error">목록을 불러오지 못했습니다: {error}</p>}

      {banner && (
        <div className={`action-banner ${banner.tone}`} role="status">
          <strong>{banner.title}</strong>
          {banner.tone === 'refused' && banner.code && <code>{banner.code}</code>}
          <span>{banner.detail}</span>
          <button type="button" className="btn btn-ghost small" onClick={() => setBanner(null)}>닫기</button>
        </div>
      )}

      <div className="queue-layout">
        <div className="queue-list" role="list" aria-label={`${QUEUE_LABEL[tab]} 목록`}>
          <div className="queue-list-head">
            <strong>{QUEUE_LABEL[tab]}</strong>
            <span>{items.length}건</span>
          </div>
          {!loading && items.length === 0 && (
            <p className="queue-empty">
              {tab === 'awaiting_approval' && '승인 대기 중인 요청이 없습니다. 요청 접수에서 새 요청을 만들 수 있습니다.'}
              {tab === 'ready_to_execute' && '예약을 보유한 실행 대기 요청이 없습니다.'}
              {tab === 'needs_review' && '재검토가 필요한 요청이 없습니다.'}
            </p>
          )}
          {items.map((item) => {
            const forecast = tab === 'ready_to_execute' ? 'ok' : approvalForecast(item)
            return (
              <button
                type="button"
                role="listitem"
                key={item.proposal_id}
                className={current?.proposal_id === item.proposal_id ? 'queue-row on' : 'queue-row'}
                onClick={() => setSelected(item.proposal_id)}
              >
                <span className="queue-row-top">
                  <strong>{item.requester}</strong>
                  <span className="mono">#{item.request_id}</span>
                </span>
                <span className="queue-row-asset">{item.asset_name} × {item.payload.qty}</span>
                <span className="queue-row-tags">
                  <span className="tag">v{item.version}</span>
                  {item.reservation_status && <span className={`tag ${item.reservation_status}`}>예약 {item.reservation_status}</span>}
                  {forecast !== 'ok' && <span className="tag warn">수량 부족 예상</span>}
                </span>
              </button>
            )
          })}
        </div>

        {current ? (
          <ProposalDetail
            key={current.proposal_id}
            state={tab}
            item={current}
            operator={operator}
            busy={busy}
            trace={traces[current.request_id]}
            onAction={(kind, reason) => void onAction(kind, reason)}
          />
        ) : (
          <div className="detail-empty">
            <strong>선택된 요청이 없습니다</strong>
            <p>왼쪽 목록에서 요청을 고르면 처리안, 수량 통제, 처리 흐름, Agent 실행 기록을 함께 볼 수 있습니다.</p>
          </div>
        )}
      </div>
    </section>
  )
}
