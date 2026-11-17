// 보유 = 다른 요청 예약 + 이 요청 예약 + 예약 가능. 승인 대기면 예약될 수량을 미리 표시한다.

type Props = {
  label: string
  total: number
  heldByOthers: number
  heldByThis: number
  preview: number
  totalLabel: string
}

export function CapacityBar({ label, total, heldByOthers, heldByThis, preview, totalLabel }: Props) {
  const free = Math.max(total - heldByOthers - heldByThis, 0)
  const previewFits = Math.min(preview, free)
  const shortage = Math.max(preview - free, 0)
  const scale = Math.max(total, heldByOthers + heldByThis + preview, 1)
  const pct = (value: number) => `${(value / scale) * 100}%`

  return (
    <div className="capacity">
      <div className="capacity-head">
        <strong>{label}</strong>
        <span>
          {totalLabel} <b>{total}</b>
        </span>
      </div>
      <div className="capacity-track" role="img" aria-label={`${label}: ${totalLabel} ${total}, 다른 요청 예약 ${heldByOthers}, 이 요청 예약 ${heldByThis}, 예약 가능 ${free}`}>
        {heldByOthers > 0 && <span className="seg seg-others" style={{ width: pct(heldByOthers) }} />}
        {heldByThis > 0 && <span className="seg seg-this" style={{ width: pct(heldByThis) }} />}
        {previewFits > 0 && <span className="seg seg-preview" style={{ width: pct(previewFits) }} />}
        {shortage > 0 && <span className="seg seg-shortage" style={{ width: pct(shortage) }} />}
      </div>
      <ul className="capacity-legend">
        <li><i className="seg-others" />다른 요청 예약 {heldByOthers}</li>
        {heldByThis > 0 && <li><i className="seg-this" />이 요청 예약 {heldByThis}</li>}
        {preview > 0 && <li><i className="seg-preview" />승인 시 예약 {preview}</li>}
        <li><i className="seg-free" />예약 가능 {free}</li>
        {shortage > 0 && <li className="legend-warn"><i className="seg-shortage" />부족 {shortage}</li>}
      </ul>
    </div>
  )
}
