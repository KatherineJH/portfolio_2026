import { type FormEvent, useCallback, useEffect, useState } from 'react'
import {
  decide,
  execute,
  getRun,
  getTrace,
  listRuns,
  listPending,
  releaseProposal,
  reReviewProposal,
  submitAssetRequest,
  type IntakeResponse,
  type PendingProposal,
  type RunNode,
  type RunSummary,
  type TraceRow,
} from './api'
import './App.css'

const DEMO_PERSONAS = [
  { id: 1, name: 'kim', role: 'Employee', initials: 'KI' },
  { id: 2, name: 'lee', role: 'Employee', initials: 'LE' },
  { id: 3, name: 'park', role: 'Employee', initials: 'PA' },
] as const

const DEMO_OPERATORS = [
  { id: 4, name: 'op-song', role: 'IT Operator · Approver', initials: 'OS' },
  { id: 5, name: 'op-jung', role: 'IT Operator · Viewer', initials: 'OJ' },
] as const

type ChatEntry =
  | { id: string; role: 'user'; content: string; persona: string }
  | { id: string; role: 'assistant'; content: string; result: IntakeResponse }

const ROUTE_COPY: Record<IntakeResponse['route'], { label: string; tone: string }> = {
  propose: { label: '처리안 생성', tone: 'success' },
  need_info: { label: '추가 정보 필요', tone: 'info' },
  need_review: { label: '담당자 검토 필요', tone: 'warning' },
  escalate: { label: '담당자 이관', tone: 'danger' },
}

function App() {
  const [view, setView] = useState<'intake' | 'approvals' | 'observability'>('intake')
  const [personaId, setPersonaId] = useState(1)
  const [accountOpen, setAccountOpen] = useState(false)
  const [draft, setDraft] = useState('')
  const [chatBusy, setChatBusy] = useState(false)
  const [chatError, setChatError] = useState<string | null>(null)
  const [chatEntries, setChatEntries] = useState<ChatEntry[]>([])
  const [runs, setRuns] = useState<RunSummary[]>([])
  const [runNodes, setRunNodes] = useState<Record<string, RunNode[]>>({})
  const [openRun, setOpenRun] = useState<string | null>(null)
  const [runsLoading, setRunsLoading] = useState(false)
  const [runsError, setRunsError] = useState<string | null>(null)
  const [items, setItems] = useState<PendingProposal[]>([])
  const [operatorId, setOperatorId] = useState(4)
  const [busy, setBusy] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [tab, setTab] = useState<'awaiting_approval' | 'ready_to_execute' | 'needs_review'>(
  'awaiting_approval',
  )
  const [openTrace, setOpenTrace] = useState<number | null>(null)
  const [traces, setTraces] = useState<Record<number, TraceRow[]>>({})
  const employeePersona = DEMO_PERSONAS.find((item) => item.id === personaId) ?? DEMO_PERSONAS[0]
  const activePersonas = view === 'intake' ? DEMO_PERSONAS : DEMO_OPERATORS
  const activePersona = view === 'intake'
    ? employeePersona
    : DEMO_OPERATORS.find((item) => item.id === operatorId) ?? DEMO_OPERATORS[0]

  async function onSubmitRequest(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const message = draft.trim()
    if (!message || chatBusy) return

    setChatEntries((entries) => [
      ...entries,
      { id: crypto.randomUUID(), role: 'user', content: message, persona: employeePersona.name },
    ])
    setDraft('')
    setChatError(null)
    setChatBusy(true)

    try {
      const result = await submitAssetRequest(message, employeePersona.id)
      setChatEntries((entries) => [
        ...entries,
        {
          id: crypto.randomUUID(),
          role: 'assistant',
          content: result.reason,
          result,
        },
      ])
    } catch (e) {
      setChatError(e instanceof Error ? e.message : String(e))
    } finally {
      setChatBusy(false)
    }
  }

  function fillExample(message: string) {
    setDraft(message)
  }

  const refreshRuns = useCallback(async () => {
    setRunsLoading(true)
    setRunsError(null)
    try {
      const data = await listRuns()
      setRuns(data.items)
    } catch (e) {
      setRunsError(e instanceof Error ? e.message : String(e))
    } finally {
      setRunsLoading(false)
    }
  }, [])

  async function onToggleRun(runId: string) {
    if (openRun === runId) {
      setOpenRun(null)
      return
    }
    setOpenRun(runId)
    if (runNodes[runId]) return
    try {
      const data = await getRun(runId)
      setRunNodes((current) => ({ ...current, [runId]: data.items }))
    } catch (e) {
      setRunsError(e instanceof Error ? e.message : String(e))
    }
  }

  const refresh = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const data = await listPending(tab)
      setItems(data.items)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [tab])

  // onDecide 핸들러 추가
  async function onDecide(proposalId: number, choice: 'approve' | 'reject') {
    setBusy(proposalId)
    setError(null)
    try {
      await decide(proposalId, choice, operatorId)
      await refresh()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(null)
    }
  }

  // 실행 핸들러 추가
  async function onExecute(item: PendingProposal) {
    setBusy(item.proposal_id)
    setError(null)
    try {
      await execute(item.request_id, item.version, operatorId)
      await refresh()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(null)
    }
  }

  async function onRelease(item: PendingProposal) {
    if (busy !== null) return
    const reason = window.prompt('예약을 해제하는 사유를 입력해 주세요.')?.trim()
    if (!reason || !window.confirm('이 예약을 해제하고 요청을 재검토 상태로 바꿀까요?')) return

    setBusy(item.proposal_id)
    setError(null)
    try {
      await releaseProposal(item.proposal_id, reason, operatorId)
      await refresh()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(null)
    }
  }

  async function onReReview(item: PendingProposal) {
    if (busy !== null) return
    const reason = window.prompt('재검토 사유를 입력해 주세요.')?.trim()
    if (!reason || !window.confirm('이 요청을 다시 승인 대기 상태로 보낼까요?')) return

    setBusy(item.proposal_id)
    setError(null)
    try {
      await reReviewProposal(item.proposal_id, reason, operatorId)
      await refresh()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(null)
    }
  }

  // Trace Toggle 핸들러 추가
  async function onToggleTrace(item: PendingProposal) {
    if (openTrace === item.request_id) {
      setOpenTrace(null)
      return
    }
    setOpenTrace(item.request_id)
    if (traces[item.request_id]) return
    try {
      const data = await getTrace(item.request_id)
      setTraces((prev) => ({ ...prev, [item.request_id]: data.items }))
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  useEffect(() => {
    const timer = window.setTimeout(() => void refresh(), 0)
    return () => window.clearTimeout(timer)
  }, [refresh])

  useEffect(() => {
    if (view !== 'observability') return
    const timer = window.setTimeout(() => void refreshRuns(), 0)
    return () => window.clearTimeout(timer)
  }, [refreshRuns, view])

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark">AF</span>
          <div>
            <strong>AssetFlow</strong>
            <span>IT Asset Operations</span>
          </div>
        </div>

        <nav className="primary-nav" aria-label="주요 메뉴">
          <button
            className={view === 'intake' ? 'nav-item active' : 'nav-item'}
            onClick={() => setView('intake')}
          >
            <span className="nav-icon" aria-hidden="true">+</span>
            요청 접수
          </button>
          <button
            className={view === 'approvals' ? 'nav-item active' : 'nav-item'}
            onClick={() => setView('approvals')}
          >
            <span className="nav-icon" aria-hidden="true">✓</span>
            승인 관리
          </button>
          <button
            className={view === 'observability' ? 'nav-item active' : 'nav-item'}
            onClick={() => setView('observability')}
          >
            <span className="nav-icon" aria-hidden="true">↗</span>
            처리 관측
          </button>
        </nav>

        <div className="sidebar-foot">
          <span className="status-dot" />
          AI workflow online
        </div>
      </aside>

      <main className="workspace">
        <div className="account-bar">
          <div className="demo-notice">
            <span>Demo environment</span>
            <strong>Persona switching enabled</strong>
          </div>
          <div className="account-menu">
            <button
              className="account-trigger"
              type="button"
              aria-haspopup="menu"
              aria-expanded={accountOpen}
              onClick={() => setAccountOpen((open) => !open)}
            >
              <span className="avatar">{activePersona.initials}</span>
              <span className="account-copy">
                <strong>{activePersona.name}</strong>
                <span>{activePersona.role}</span>
              </span>
              <span className="chevron" aria-hidden="true">⌄</span>
            </button>

            {accountOpen && (
              <div className="account-popover" role="menu">
                <div className="popover-heading">
                  <span>Demo persona</span>
                  <small>Not production authentication</small>
                </div>
                {activePersonas.map((item) => (
                  <button
                    key={item.id}
                    type="button"
                    role="menuitem"
                    className={item.id === activePersona.id ? 'persona-row selected' : 'persona-row'}
                    onClick={() => {
                      if (view === 'intake') {
                        setPersonaId(item.id)
                      } else {
                        setOperatorId(item.id)
                      }
                      setAccountOpen(false)
                    }}
                  >
                    <span className="avatar small">{item.initials}</span>
                    <span>
                      <strong>{item.name}</strong>
                      <small>{item.role} · ID {item.id}</small>
                    </span>
                    {item.id === activePersona.id && <span className="selected-mark">✓</span>}
                  </button>
                ))}
              </div>
            )}
          </div>
        </div>

        {view === 'intake' && (
          <section className="intake-view">
            <span className="eyebrow">Employee workspace</span>
            <h1>자산 요청 접수</h1>
            <p>자연어로 자산 교체 요청을 접수하고 정책 근거가 포함된 처리 결과를 확인합니다.</p>

            <div className="chat-layout">
              <div className="chat-panel">
                <div className="chat-head">
                  <div>
                    <span className="assistant-avatar">AI</span>
                    <div>
                      <strong>AssetFlow Assistant</strong>
                      <span>Policy-grounded request intake</span>
                    </div>
                  </div>
                  <span className="model-state"><i /> Online</span>
                </div>

                <div className="chat-stream" aria-live="polite">
                  <div className="message assistant-message">
                    <span className="message-author">AssetFlow</span>
                    <p>
                      안녕하세요, {employeePersona.name}님. 지급받은 IT 자산의 교체 요청을 말씀해 주세요.
                      정책과 현재 자산 상태를 확인해 처리 경로를 안내합니다.
                    </p>
                  </div>

                  {chatEntries.map((entry) => {
                    if (entry.role === 'user') {
                      return (
                        <div key={entry.id} className="message user-message">
                          <span className="message-author">{entry.persona}</span>
                          <p>{entry.content}</p>
                        </div>
                      )
                    }

                    const route = ROUTE_COPY[entry.result.route]
                    return (
                      <div key={entry.id} className="message assistant-message result-message">
                        <div className="result-topline">
                          <span className="message-author">AssetFlow</span>
                          <span className={`route-badge ${route.tone}`}>{route.label}</span>
                        </div>
                        <p>{entry.content}</p>
                        {entry.result.missing.length > 0 && (
                          <div className="result-section">
                            <strong>확인이 필요한 정보</strong>
                            <span>{entry.result.missing.join(', ')}</span>
                          </div>
                        )}
                        <div className="result-section">
                          <strong>정책 근거</strong>
                          <div className="policy-list">
                            {entry.result.policy_refs.length === 0 ? (
                              <span className="muted">적용된 정책 근거 없음</span>
                            ) : (
                              entry.result.policy_refs.map((ref) => (
                                <span className="policy-chip" key={`${ref.policy_key}-${ref.version}`}>
                                  {ref.policy_key} · v{ref.version}
                                </span>
                              ))
                            )}
                          </div>
                        </div>
                        <div className="request-meta">
                          <span>Request #{entry.result.request_id}</span>
                          <span>Run {entry.result.run_id.slice(0, 8)}</span>
                          {entry.result.proposal_id && (
                            <button type="button" onClick={() => setView('approvals')}>
                              승인 관리에서 보기 →
                            </button>
                          )}
                        </div>
                      </div>
                    )
                  })}

                  {chatBusy && (
                    <div className="message assistant-message typing-message">
                      <span /><span /><span />
                      <p>자산과 정책을 확인하고 있습니다.</p>
                    </div>
                  )}
                </div>

                {chatError && <div className="chat-error">요청을 처리하지 못했습니다: {chatError}</div>}

                <form className="chat-composer" onSubmit={onSubmitRequest}>
                  <textarea
                    value={draft}
                    onChange={(event) => setDraft(event.target.value)}
                    onKeyDown={(event) => {
                      if (event.key === 'Enter' && !event.shiftKey) {
                        event.preventDefault()
                        event.currentTarget.form?.requestSubmit()
                      }
                    }}
                    rows={2}
                    maxLength={1000}
                    placeholder="예: 지급받은 USB-C 독 2개가 모두 고장 나서 교체가 필요합니다."
                    aria-label="자산 요청 내용"
                  />
                  <div className="composer-foot">
                    <span>Enter 전송 · Shift + Enter 줄바꿈</span>
                    <button type="submit" disabled={!draft.trim() || chatBusy}>
                      {chatBusy ? '처리 중' : '요청 보내기'}
                    </button>
                  </div>
                </form>
              </div>

              <aside className="scenario-panel">
                <span className="eyebrow">Demo scenarios</span>
                <h2>{employeePersona.name}의 요청 예시</h2>
                <p>현재 페르소나의 지급 자산에 맞는 문장을 선택해 보세요.</p>
                {employeePersona.id === 1 && (
                  <>
                    <button type="button" onClick={() => fillExample('지급받은 USB-C 독 2개가 모두 고장 나서 교체가 필요합니다.')}>독 2개 교체 요청</button>
                    <button type="button" onClick={() => fillExample('노트북이 고장 난 것 같아서 교체하고 싶습니다.')}>점검 전 노트북 요청</button>
                  </>
                )}
                {employeePersona.id === 2 && (
                  <>
                    <button type="button" onClick={() => fillExample('지급받은 모니터 2개를 모두 교체해 주세요.')}>모니터 2개 교체 요청</button>
                    <button type="button" onClick={() => fillExample('맥북이 작동하지 않아 교체가 필요합니다.')}>점검 전 맥북 요청</button>
                  </>
                )}
                {employeePersona.id === 3 && (
                  <>
                    <button type="button" onClick={() => fillExample('갤럭시 휴대폰이 고장 나서 교체가 필요합니다.')}>휴대폰 교체 요청</button>
                    <button type="button" onClick={() => fillExample('USB-C 독이 고장 나서 교체하고 싶습니다.')}>점검 반려 자산 요청</button>
                  </>
                )}
                <div className="guardrail-note">
                  <strong>AI 권한 범위</strong>
                  <p>AI는 처리안을 제안하지만 승인하거나 자산을 지급하지 않습니다.</p>
                </div>
              </aside>
            </div>
          </section>
        )}

        {view === 'observability' && (
          <section className="observability-view">
            <div className="observability-heading">
              <div>
                <span className="eyebrow">Workflow observability</span>
                <h1>처리 관측</h1>
                <p>실행별 지연, 모델 사용량, 비용과 노드 처리 결과를 확인합니다.</p>
              </div>
              <button type="button" onClick={refreshRuns} disabled={runsLoading}>
                {runsLoading ? '불러오는 중' : '새로고침'}
              </button>
            </div>

            {runsError && <p className="error">오류: {runsError}</p>}
            {!runsLoading && !runsError && runs.length === 0 && (
              <div className="observability-empty">아직 기록된 워크플로 실행이 없습니다.</div>
            )}

            <div className="run-list">
              {runs.map((run) => (
                <article key={run.run_id} className="run-card">
                  <button className="run-summary" type="button" onClick={() => onToggleRun(run.run_id)}>
                    <div className="run-identity">
                      <span className={run.has_failure ? 'run-state failed' : 'run-state success'}>
                        {run.has_failure ? '실패 포함' : '정상 완료'}
                      </span>
                      <strong>Run {run.run_id.slice(0, 8)}</strong>
                      <time>{new Date(run.started_at).toLocaleString('ko-KR')}</time>
                    </div>
                    <dl className="run-metrics">
                      <div><dt>노드</dt><dd>{run.node_count}</dd></div>
                      <div><dt>총 지연</dt><dd>{run.total_latency_ms.toLocaleString()}ms</dd></div>
                      <div><dt>토큰</dt><dd>{(run.prompt_tokens + run.completion_tokens).toLocaleString()}</dd></div>
                      <div><dt>비용</dt><dd>${Number(run.cost_usd).toFixed(6)}</dd></div>
                    </dl>
                    <span className="run-toggle">{openRun === run.run_id ? '접기' : '상세 보기'}</span>
                  </button>

                  {openRun === run.run_id && (
                    <div className="node-timeline">
                      {(runNodes[run.run_id] ?? []).map((node, index) => (
                        <div className="node-row" key={`${node.node_name}-${node.attempt_no}-${index}`}>
                          <span className={node.outcome === 'failed' ? 'node-dot failed' : 'node-dot'} />
                          <div className="node-copy">
                            <div>
                              <strong>{node.node_name}</strong>
                              <span>{node.outcome}</span>
                            </div>
                            {node.error_reason && <p>{node.error_reason}</p>}
                          </div>
                          <div className="node-stats">
                            <span>{node.latency_ms ?? 0}ms</span>
                            <span>{node.model ?? 'server'}</span>
                            <span>
                              {node.prompt_tokens == null
                                ? '토큰 없음'
                                : `${node.prompt_tokens + (node.completion_tokens ?? 0)} tokens`}
                            </span>
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </article>
              ))}
            </div>
          </section>
        )}

        {view === 'approvals' && (
          <section>
            <div className="page-heading">
              <div>
                <span className="eyebrow">Operator workspace</span>
                <h1>승인 관리</h1>
              </div>
            </div>

      <header className="approval-toolbar">
        <nav className="tabs">
          <button
            className={tab === 'awaiting_approval' ? 'tab on' : 'tab'}
            onClick={() => setTab('awaiting_approval')}
          >
            승인 대기
          </button>
          <button
            className={tab === 'ready_to_execute' ? 'tab on' : 'tab'}
            onClick={() => setTab('ready_to_execute')}
          >
            실행 대기
          </button>
          <button
            className={tab === 'needs_review' ? 'tab on' : 'tab'}
            onClick={() => setTab('needs_review')}
          >
            재검토
          </button>
        </nav>

        <button onClick={refresh} disabled={loading}>
          {loading ? '불러오는 중' : '새로고침'}
        </button>
      </header>

      {error && <p className="error">오류: {error}</p>}

      {!loading && !error && items.length === 0 && (
        <p className="empty">
          {tab === 'awaiting_approval'
            ? '승인 대기 중인 요청이 없습니다.'
            : tab === 'ready_to_execute'
              ? '실행 대기 중인 요청이 없습니다.'
              : '재검토가 필요한 요청이 없습니다.'}
        </p>
      )}


      <ul className="cards">
        {items.map((item) => (
          <li key={item.proposal_id} className="card">
            <div className="card-head">
              <strong>{item.requester}</strong>
              <span className="asset">
                {item.asset_name} ({item.asset_code})
              </span>
            </div>
            <dl className="facts">
              <dt>처리안</dt>
              <dd>{item.payload.qty}개 교체</dd>

              <dt>지급 / 기처리</dt>
              <dd>
                {item.qty}개 / {item.allocated_qty}개
              </dd>

              <dt>지급 가능 / 예약</dt>
              <dd>{item.reservable_item_qty}개 / {item.held_item_qty}개</dd>

              <dt>재고 보유 / 예약</dt>
              <dd>{item.on_hand_qty}개 / {item.held_stock_qty}개</dd>

              <dt>승인 가능 재고</dt>
              <dd>{item.reservable_stock_qty}개</dd>

              {item.reservation_status && (
                <>
                  <dt>현재 예약</dt>
                  <dd>{item.reservation_qty}개 · {item.reservation_status}</dd>
                </>
              )}

              <dt>근거 규정</dt>
              <dd>
                {item.policy_refs.length === 0
                  ? '없음'
                  : item.policy_refs
                      .map((p) => `${p.policy_key} v${p.version}`)
                      .join(', ')}
              </dd>
            </dl>
            <div className="actions">
              {tab === 'awaiting_approval' ? (
                <>
                  <button
                    onClick={() => onDecide(item.proposal_id, 'approve')}
                    disabled={busy !== null}
                  >
                    승인
                  </button>
                  <button
                    className="secondary"
                    onClick={() => onDecide(item.proposal_id, 'reject')}
                    disabled={busy !== null}
                  >
                    거절
                  </button>
                </>
              ) : tab === 'ready_to_execute' ? (
                <>
                  <button onClick={() => onExecute(item)} disabled={busy !== null}>
                    실행
                  </button>
                  <button className="secondary" onClick={() => onRelease(item)} disabled={busy !== null}>
                    예약 해제
                  </button>
                </>
              ) : (
                <button onClick={() => onReReview(item)} disabled={busy !== null}>
                  재검토 요청
                </button>
              )}
              <button className="secondary" onClick={() => onToggleTrace(item)} disabled={busy !== null}>
                {openTrace === item.request_id ? '처리 과정 닫기' : '처리 과정'}
              </button>
            </div>
            {openTrace === item.request_id && (
              <table className="trace">
                <thead>
                  <tr>
                    <th>노드</th>
                    <th>결과</th>
                    <th>지연</th>
                    <th>모델</th>
                    <th>토큰</th>
                    <th>비용</th>
                  </tr>
                </thead>
                <tbody>
                  {(traces[item.request_id] ?? []).map((row, i) => (
                    <tr key={i}>
                      <td>{row.node_name}</td>
                      <td>{row.outcome}</td>
                      <td>{row.latency_ms ?? '-'}ms</td>
                      <td>{row.model ?? '-'}</td>
                      <td>
                        {row.prompt_tokens == null
                          ? '-'
                          : `${row.prompt_tokens} + ${row.completion_tokens}`}
                      </td>
                      <td>{row.cost_usd == null ? '-' : `$${Number(row.cost_usd).toFixed(6)}`}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </li>
        ))}
      </ul>
          </section>
        )}
      </main>
    </div>
  )
}

export default App
