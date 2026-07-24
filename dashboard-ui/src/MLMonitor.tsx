import { useState, useEffect } from 'react'

interface MLInfo {
  threshold: number
  model_loaded: boolean
  recent_scores: number[]
  score_distribution: Record<string, number>
  total_scored: number
  approved_by_ml: number
  rejected_by_ml: number
}

export default function MLMonitor() {
  const [info, setInfo] = useState<MLInfo | null>(null)

  useEffect(() => {
    const fetchInfo = () => {
      fetch('/api/ml/info')
        .then(r => r.json())
        .then(setInfo)
        .catch(() => {})
    }
    fetchInfo()
    const interval = setInterval(fetchInfo, 10000)
    return () => clearInterval(interval)
  }, [])

  if (!info) {
    return <div className="p-4 text-[#4a4060] animate-pulse">Loading ML stats...</div>
  }

  const threshold = info.threshold ?? 0
  const totalScored = info.total_scored ?? 0
  const approvedByMl = info.approved_by_ml ?? 0
  const rejectedByMl = info.rejected_by_ml ?? 0
  const scoreDistribution = info.score_distribution ?? {}
  const recentScores = info.recent_scores ?? []

  const maxBucket = Math.max(1, ...Object.values(scoreDistribution))
  const buckets = Object.entries(scoreDistribution).sort()
  const recentNewestFirst = [...recentScores].reverse()

  return (
    <div className="h-full overflow-auto px-4 sm:px-6 py-4 font-mono text-[10px] sm:text-xs">
      {/* Header */}
      <div className="mb-3 sm:mb-4">
        <h2 className="text-[#D0CFCC] text-sm font-bold mb-2">🤖 ML Gate Monitor</h2>
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 sm:gap-3">
          <div className="bg-[#1a1528] border border-[#2a2240] p-2">
            <div className="text-[#4a4060]">Threshold</div>
            <div className="text-[#D0CFCC] text-lg font-bold">{threshold.toFixed(2)}</div>
          </div>
          <div className="bg-[#1a1528] border border-[#2a2240] p-2">
            <div className="text-[#4a4060]">Model</div>
            <div className={info.model_loaded ? 'text-[#D0CFCC]' : 'text-red-400'}>
              {info.model_loaded ? 'LOADED' : 'MISSING'}
            </div>
          </div>
          <div className="bg-[#1a1528] border border-[#2a2240] p-2">
            <div className="text-[#4a4060]">Total Scored</div>
            <div className="text-[#D0CFCC] text-lg">{totalScored}</div>
          </div>
          <div className="bg-[#1a1528] border border-[#2a2240] p-2">
            <div className="text-[#4a4060]">Approval Rate</div>
            <div className="text-[#D0CFCC] text-lg">
              {totalScored > 0
                ? ((approvedByMl / totalScored) * 100).toFixed(1) + '%'
                : '—'}
            </div>
          </div>
        </div>
      </div>

      {/* Score Distribution Histogram */}
      <div className="mb-4">
        <h3 className="text-[#4a4060] mb-2">Score Distribution (last {totalScored})</h3>
        <div className="space-y-1">
          {buckets.map(([bucket, count]) => (
            <div key={bucket} className="flex items-center gap-2">
              <span className="w-12 text-right text-[#4a4060] shrink-0">{bucket}</span>
              <div className="flex-1 bg-[#1a1528] h-4 border border-[#2a2240] relative">
                <div
                  className="absolute inset-y-0 left-0 bg-[#D0CFCC]/15 border-r border-[#D0CFCC]/20"
                  style={{ width: `${(count / maxBucket) * 100}%` }}
                />
                <span className="absolute inset-0 flex items-center px-1 text-[10px] text-[#D0CFCC]/60">
                  {count}
                </span>
              </div>
            </div>
          ))}
        </div>
      </div>

      {/* Recent Scores */}
      <div className="mb-4">
        <h3 className="text-[#4a4060] mb-2">Recent ML Scores</h3>
        <div className="flex flex-wrap gap-1">
          {recentNewestFirst.map((score, i) => {
            const isApproved = score >= threshold
            return (
              <span
                key={i}
                className={`px-1.5 py-0.5 text-[10px] border ${
                  isApproved
                    ? 'border-[#D0CFCC]/30 text-[#D0CFCC] bg-[#D0CFCC]/5'
                    : 'border-red-800 text-red-400 bg-red-950/30'
                }`}
              >
                {score.toFixed(3)}
              </span>
            )
          })}
        </div>
      </div>

      {/* Decision Breakdown */}
      <div>
        <h3 className="text-[#4a4060] mb-2">ML Decision Breakdown</h3>
        <div className="flex gap-4">
          <div className="bg-[#1a1528] border border-[#D0CFCC]/20 p-3 flex-1">
            <div className="text-[#D0CFCC] text-2xl font-bold">{approvedByMl}</div>
            <div className="text-[#4a4060] text-[10px]">APPROVED BY ML</div>
          </div>
          <div className="bg-[#1a1528] border border-red-900/50 p-3 flex-1">
            <div className="text-red-400 text-2xl font-bold">{rejectedByMl}</div>
            <div className="text-[#4a4060] text-[10px]">REJECTED BY ML</div>
          </div>
        </div>
      </div>
    </div>
  )
}
