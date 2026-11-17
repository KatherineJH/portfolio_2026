import { type FormEvent, useCallback, useEffect, useState } from 'react'
import {
  getRun,
  listRuns,
  submitAssetRequest,
  type IntakeResponse,
  type RunNode,
  type RunSummary,
} from './api'
import { ApprovalsView } from './approvals/ApprovalsView'
import type { Operator } from './approvals/model'
import './App.css'

const DEMO_PERSONAS = [
  { id: 1, name: 'kim', role: 'Employee', initials: 'KI' },
  { id: 2, name: 'lee', role: 'Employee', initials: 'LE' },
  { id: 3, name: 'park', role: 'Employee', initials: 'PA' },
] as const

// 시드 데이터의 can_approve 와 같다. 화면은 버튼 상태에만 쓰고, 권한 판단은 서버가 한다.
const DEMO_OPERATORS: readonly Operator[] = [
  { id: 4, name: 'op-song', role: 'IT Operator · Approver', initials: 'OS', canApprove: true },
  { id: 5, name: 'op-jung', role: 'IT Operator · Viewer', initials: 'OJ', canApprove: false },
]

type ChatEntry =
  | { id: string; role: 'user'; content: string; persona: string }
  | { id: string; role: 'assistant'; content: string; result: IntakeResponse }

const ROUTE_COPY: Record<IntakeResponse['route'], { label: string; tone: string }> = {
  propose: { label: '처리안 생성', tone: 'success' },
  need_info: { label: '추가 정보 필요', tone: 'info' },
  need_review: { label: '담당자 검토 필요', tone: 'warning' },
  escalate: { label: '담당자 이관', tone: 'danger' },
}

// 인용이 비는 이유는 코드가 정한다 (pick_policy_refs). 화면은 그 이유를 그대로 설명한다.
function citationGap(result: IntakeResponse): string {
  if (result.target_item_id === null) {
    return '대상 자산이 특정되지 않아 규정을 인용하지 않았습니다.'
  }
  return '검색 후보에 이 처리 경로의 주제 규정이 없어 인용을 보류했습니다. 다른 규정을 대신 인용하지 않습니다.'
}

const WORKFLOW_NODES = [
  { name: 'load', copy: '임직원의 지급 자산·미처리 수량·점검 상태 조회' },
  { name: 'retrieve', copy: '요청문과 가까운 규정 후보 검색 (pgvector)' },
  { name: 'decide', copy: 'LLM이 대상 자산과 귀책 주제를 판단하고, 처리 경로는 서버 규칙이 결정' },
]

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
  const [operatorId, setOperatorId] = useState(4)
  const employeePersona = DEMO_PERSONAS.find((item) => item.id === personaId) ?? DEMO_PERSONAS[0]
  const operator = DEMO_OPERATORS.find((item) => item.id === operatorId) ?? DEMO_OPERATORS[0]
  const activePersonas = view === 'intake' ? DEMO_PERSONAS : DEMO_OPERATORS
  const activePersona = view === 'intake' ? employeePersona : operator

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
      const data = await listRuns(operatorId)
      setRuns(data.items)
    } catch (e) {
      setRunsError(e instanceof Error ? e.message : String(e))
    } finally {
      setRunsLoading(false)
    }
  }, [operatorId])

  async function onToggleRun(runId: string) {
    if (openRun === runId) {
      setOpenRun(null)
      return
    }
    setOpenRun(runId)
    if (runNodes[runId]) return
    try {
      const data = await getRun(runId, operatorId)
      setRunNodes((current) => ({ ...current, [runId]: data.items }))
    } catch (e) {
      setRunsError(e instanceof Error ? e.message : String(e))
    }
  }

  useEffect(() => {
    if (view !== 'observability') return
    const timer = window.setTimeout(() => void refreshRuns(), 0)
    return () => window.clearTimeout(timer)
  }, [refreshRuns, view])

  const latencies = runs.map((run) => run.total_latency_ms)
  const runStats = {
    count: runs.length,
    avgLatency: runs.length ? Math.round(latencies.reduce((a, b) => a + b, 0) / runs.length) : 0,
    maxLatency: runs.length ? Math.max(...latencies) : 0,
    tokens: runs.reduce((sum, run) => sum + run.prompt_tokens + run.completion_tokens, 0),
    cost: runs.reduce((sum, run) => sum + Number(run.cost_usd), 0),
    failed: runs.filter((run) => run.has_failure).length,
  }

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
          <span className="nav-group">Agent</span>
          <button
            className={view === 'intake' ? 'nav-item active' : 'nav-item'}
            onClick={() => setView('intake')}
          >
            <span className="nav-icon" aria-hidden="true">+</span>
            요청 접수
          </button>
          <span className="nav-group">Control · Observe</span>
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
            <span className="eyebrow">Employee workspace · Agent workflow</span>
            <h1>자산 요청 접수</h1>
            <p>자연어 요청을 Agent 워크플로가 처리합니다. 규정 검색으로 근거를 찾고, 처리 경로는 서버 규칙이 정합니다.</p>

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
                              <span className="citation-gap">{citationGap(entry.result)}</span>
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
                <div className="workflow-card">
                  <strong>Agent 워크플로 (LangGraph)</strong>
                  <ol>
                    {WORKFLOW_NODES.map((node) => (
                      <li key={node.name}>
                        <code>{node.name}</code>
                        <span>{node.copy}</span>
                      </li>
                    ))}
                  </ol>
                </div>
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
            <div className="view-heading">
              <div>
                <span className="eyebrow">Operator workspace · Workflow observability</span>
                <h1>처리 관측</h1>
                <p>Agent 실행별 지연, 모델 사용량, 비용과 노드 처리 결과를 확인합니다. IT 담당자만 조회할 수 있습니다.</p>
              </div>
              <button type="button" className="btn btn-secondary" onClick={() => void refreshRuns()} disabled={runsLoading}>
                {runsLoading ? '불러오는 중' : '새로고침'}
              </button>
            </div>

            <dl className="obs-summary" aria-label="최근 실행 요약">
              <div><dt>최근 실행</dt><dd>{runStats.count}</dd><small>최대 20건</small></div>
              <div><dt>평균 지연</dt><dd>{runStats.avgLatency.toLocaleString()}<small>ms</small></dd><small>최대 {runStats.maxLatency.toLocaleString()}ms</small></div>
              <div><dt>총 토큰</dt><dd>{runStats.tokens.toLocaleString()}</dd><small>prompt + completion</small></div>
              <div><dt>총 비용</dt><dd>${runStats.cost.toFixed(6)}</dd><small>모델 단가 기준 추정</small></div>
              <div className={runStats.failed > 0 ? 'warn' : ''}><dt>실패 포함 실행</dt><dd>{runStats.failed}</dd><small>노드 실패가 있는 실행</small></div>
            </dl>

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

        {/* 담당자가 바뀌면 이전 담당자의 결과 배너와 선택을 남기지 않는다. */}
        {view === 'approvals' && <ApprovalsView key={operator.id} operator={operator} />}
      </main>
    </div>
  )
}

export default App
