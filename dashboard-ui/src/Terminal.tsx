import { useState, useEffect, useRef, useCallback } from 'react'

const MAX_LINES = 5000

function classifyLine(line: string): string {
  if (line.includes('ERROR') || line.includes('🔥')) return 'log-error'
  if (line.includes('WARNING') || line.includes('⚠️')) return 'log-warning'
  if (line.includes('✅ APPROVED') || line.includes('TRADE APPROVED')) return 'log-approved'
  if (line.includes('❌ REJECTED') || line.includes('REJECTED ❌')) return 'log-rejected'
  if (line.includes('ML:') || line.includes('ML Gate')) return 'log-ml'
  return 'log-info'
}

export default function Terminal() {
  const [lines, setLines] = useState<string[]>([])
  const [filter, setFilter] = useState('')
  const [paused, setPaused] = useState(false)
  const [connected, setConnected] = useState(false)
  const containerRef = useRef<HTMLDivElement>(null)
  const autoScrollRef = useRef(true)

  // Load initial backlog
  useEffect(() => {
    fetch('/api/logs/recent?lines=200')
      .then(r => r.json())
      .then(data => setLines(data.lines || []))
      .catch(() => {})
  }, [])

  // SSE stream
  useEffect(() => {
    const es = new EventSource('/api/logs/stream')
    es.onopen = () => setConnected(true)
    es.onerror = () => setConnected(false)

    es.addEventListener('log', (e: MessageEvent) => {
      const line = e.data
      if (!line) return
      setLines(prev => {
        const next = [...prev, line]
        return next.length > MAX_LINES ? next.slice(-MAX_LINES) : next
      })
    })

    return () => {
      setConnected(false)
      es.close()
    }
  }, [])

  // Auto-scroll
  const handleScroll = useCallback(() => {
    const el = containerRef.current
    if (!el) return
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 30
    autoScrollRef.current = atBottom
  }, [])

  useEffect(() => {
    if (autoScrollRef.current && containerRef.current) {
      containerRef.current.scrollTop = containerRef.current.scrollHeight
    }
  }, [lines])

  const filtered = filter
    ? lines.filter(l => l.toLowerCase().includes(filter.toLowerCase()))
    : lines

  return (
    <div className="h-full flex flex-col">
      {/* Toolbar */}
      <div className="flex items-center gap-1 sm:gap-2 px-1.5 sm:px-2 py-1 bg-[#1a1528] border-b border-[#2a2240] text-[10px] sm:text-xs">
        <span className="text-[#4a4060] hidden sm:inline">$ tail -F apolo.log</span>
        <input
          type="text"
          value={filter}
          onChange={e => setFilter(e.target.value)}
          placeholder="grep..."
          className="flex-1 bg-[#171421] border border-[#2a2240] text-[#D0CFCC] px-1.5 sm:px-2 py-0.5 text-[10px] sm:text-xs focus:outline-none focus:border-[#D0CFCC] font-mono placeholder-[#4a4060]"
        />
        <button
          onClick={() => setPaused(!paused)}
          className={`px-1.5 sm:px-2 py-0.5 text-[10px] sm:text-xs border ${paused ? 'border-yellow-700 text-yellow-500' : 'border-[#2a2240] text-[#4a4060]'} hover:border-[#D0CFCC] whitespace-nowrap`}
        >
          {paused ? '▶' : '⏸'}
        </button>
        <span className={`w-1.5 h-1.5 sm:w-2 sm:h-2 rounded-full ${connected ? 'bg-[#D0CFCC]' : 'bg-red-500'}`} />
        <span className="text-[#4a4060] text-[9px] sm:text-xs hidden sm:inline">{filtered.length.toLocaleString()} lines</span>
      </div>

      {/* Log Output */}
      <div
        ref={containerRef}
        onScroll={handleScroll}
        className="flex-1 overflow-y-auto px-1.5 sm:px-2 py-1 font-mono text-[10px] sm:text-xs leading-relaxed"
      >
        {filtered.length === 0 && (
          <div className="text-[#4a4060] animate-pulse text-[10px] sm:text-xs">Waiting for logs...</div>
        )}
        {filtered.map((line, i) => (
          <div key={i} className={`${classifyLine(line)} break-words whitespace-pre-wrap overflow-x-hidden`}>
            {line}
          </div>
        ))}
        {!paused && <div className="cursor text-[#D0CFCC]">&nbsp;</div>}
      </div>
    </div>
  )
}
