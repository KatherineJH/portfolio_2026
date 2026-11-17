// 요청 상태와 예약 상태로 지금 위치를 보여준다. 구현된 전이만 그린다.

import type { PendingProposal } from '../api'
import type { QueueState } from './model'

type Step = { key: string; label: string; note: string }

const MAIN: Step[] = [
  { key: 'intake', label: '요청 접수', note: '임직원 자연어 요청' },
  { key: 'proposal', label: 'AI 처리안', note: '버전·digest 고정' },
  { key: 'approval', label: '승인 · 예약', note: '재고·지급 수량 확보' },
  { key: 'execute', label: '실행', note: '예약 소비 · 실행 키' },
]

function currentIndex(state: QueueState): number {
  if (state === 'ready_to_execute') return 3
  return 2
}

export function LifecycleSteps({ state, item }: { state: QueueState; item: PendingProposal }) {
  const current = currentIndex(state)
  const released = item.reservation_status === 'released'
  const inReview = state === 'needs_review'

  return (
    <div className="lifecycle">
      <ol className="lifecycle-main">
        {MAIN.map((step, index) => {
          const status = inReview && index === 2
            ? 'blocked'
            : index < current ? 'done' : index === current ? 'current' : 'todo'
          return (
            <li key={step.key} className={`step ${status}`}>
              <span className="step-dot" aria-hidden="true">{status === 'done' ? '✓' : index + 1}</span>
              <span className="step-copy">
                <strong>{step.label}</strong>
                <small>{status === 'blocked' ? (released ? '예약 해제됨' : '예약 없음') : step.note}</small>
              </span>
            </li>
          )
        })}
      </ol>
      <div className={inReview || released ? 'lifecycle-branch active' : 'lifecycle-branch'}>
        <span className="branch-label">예외 경로</span>
        <span className={released ? 'branch-step done' : 'branch-step'}>예약 해제 · 승인 철회</span>
        <span className="branch-arrow" aria-hidden="true">→</span>
        <span className={inReview ? 'branch-step current' : released ? 'branch-step done' : 'branch-step'}>재검토</span>
        <span className="branch-arrow" aria-hidden="true">→</span>
        <span className="branch-step">재승인 시 새 예약</span>
      </div>
    </div>
  )
}
