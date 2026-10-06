// API 응답의 모양. 서버가 실제로 돌려주는 필드와 일치해야 한다.
// 여기가 틀리면 편집기는 통과시키고 화면에서 undefined 가 뜬다.

export type PendingProposal = {
  proposal_id: number
  request_id: number
  version: number
  payload: { action: string; assignment_item_id: number; qty: number }
  policy_refs: { policy_key: string; version: number }[]
  created_at: string
  requester: string
  asset_code: string
  asset_name: string
  qty: number
  allocated_qty: number
  remaining_item_qty: number
  held_item_qty: number
  reservable_item_qty: number
  on_hand_qty: number
  held_stock_qty: number
  reservable_stock_qty: number
  reservation_status: 'held' | 'consumed' | 'released' | null
  reservation_qty: number | null
}

export type ProposalActionResult = {
  outcome: string
  request_id: number | null
  proposal_id: number
  reservation_id?: number | null
  reason_code?: string | null
  detail?: string | null
}

export type TraceRow = {
  node_name: string
  attempt_no: number
  latency_ms: number | null
  model: string | null
  prompt_tokens: number | null
  completion_tokens: number | null
  cost_usd: number | null
  outcome: string
  error_reason: string | null
}

export type IntakeResponse = {
  run_id: string
  route: 'propose' | 'need_info' | 'need_review' | 'escalate'
  reason: string
  target_item_id: number | null
  missing: string[]
  policy_refs: { policy_key: string; version: number }[]
  request_id: number
  proposal_id: number | null
}

export type RunSummary = {
  run_id: string
  started_at: string
  ended_at: string | null
  node_count: number
  total_latency_ms: number
  model: string | null
  prompt_tokens: number
  completion_tokens: number
  cost_usd: number
  has_failure: boolean
}

export type RunNode = TraceRow & {
  started_at: string
  ended_at: string | null
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api${path}`, init)
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    throw new Error(body.detail ?? `${response.status} ${response.statusText}`)
  }
  return response.json()
}

export function listPending(
  state = 'awaiting_approval',
): Promise<{ items: PendingProposal[] }> {
  return request(`/proposals/pending?state=${state}`)
}

export function getTrace(requestId: number): Promise<{ items: TraceRow[] }> {
  return request(`/requests/${requestId}/trace`)
}

export function decide(
  proposalId: number,
  decision: 'approve' | 'reject',
  userId: number,
): Promise<{ request_id: number; decision: string; approver_id: number }> {
  return request(`/proposals/${proposalId}/approval`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'x-user-id': String(userId) },
    body: JSON.stringify({ proposal_id: proposalId, decision }),
  })
}

export function execute(
  requestId: number,
  proposalVersion: number,
  userId: number,
): Promise<{ execution_key: string; outcome: string }> {
  return request('/executions', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'x-user-id': String(userId) },
    body: JSON.stringify({ request_id: requestId, proposal_version: proposalVersion }),
  })
}

export function releaseProposal(
  proposalId: number,
  reason: string,
  userId: number,
): Promise<ProposalActionResult> {
  return request(`/proposals/${proposalId}/release`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'x-user-id': String(userId) },
    body: JSON.stringify({ reason }),
  })
}

export function reReviewProposal(
  proposalId: number,
  reason: string,
  userId: number,
): Promise<ProposalActionResult> {
  return request(`/proposals/${proposalId}/re-review`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'x-user-id': String(userId) },
    body: JSON.stringify({ reason }),
  })
}

export function submitAssetRequest(
  message: string,
  userId: number,
): Promise<IntakeResponse> {
  return request('/requests', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'x-user-id': String(userId),
      'Idempotency-Key': crypto.randomUUID(),
    },
    body: JSON.stringify({ message }),
  })
}

export function listRuns(): Promise<{ items: RunSummary[] }> {
  return request('/observability/runs')
}

export function getRun(runId: string): Promise<{ items: RunNode[] }> {
  return request(`/observability/runs/${runId}`)
}
