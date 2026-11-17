// 승인 관리 화면이 쓰는 파생 값. 서버가 준 숫자만으로 계산하고, 최종 판단은 서버가 한다.

import type { PendingProposal } from '../api'

export type QueueState = 'awaiting_approval' | 'ready_to_execute' | 'needs_review'

export const QUEUE_LABEL: Record<QueueState, string> = {
  awaiting_approval: '승인 대기',
  ready_to_execute: '실행 대기',
  needs_review: '재검토',
}

export type Operator = {
  id: number
  name: string
  role: string
  initials: string
  canApprove: boolean
}

/** 이 요청이 지금 붙잡고 있는 예약 수량. held 일 때만 재고와 지급 수량을 점유한다. */
export function ownHeldQty(item: PendingProposal): number {
  return item.reservation_status === 'held' ? item.reservation_qty ?? 0 : 0
}

/** 승인하면 예약이 가능한지 미리 계산한다. 서버가 같은 조건을 잠금 안에서 다시 확인한다. */
export function approvalForecast(item: PendingProposal): 'ok' | 'stock' | 'item' {
  const qty = item.payload.qty
  if (qty > item.reservable_item_qty) return 'item'
  if (qty > (item.reservable_stock_qty ?? 0)) return 'stock'
  return 'ok'
}

export function shortDigest(digest: string | null | undefined): string {
  if (!digest) return '-'
  return digest.length > 12 ? `${digest.slice(0, 12)}…` : digest
}
