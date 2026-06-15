import { useState, useEffect, useRef, useCallback } from 'react'
import { Search, Pause, Play } from 'lucide-react'

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
      <div className="flex items-center gap-2 px-2 sm:px-3 py-2 bg-[#1a1528] border-b border-[#2a2240]">
        <div className="relative flex-1">
          <Search size={16} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-[#4a4060] pointer-events-none" />
          <input
            type="text"
            value={filter}
            onChange={e => setFilter(e.target.value)}
            placeholder="grep logs..."
            className="w-full bg-[#171421] border border-[#2a2240] rounded-lg text-[#D0CFCC] pl-8 pr-3 py-2 text-sm focus:outline-none focus:border-[#D0CFCC] font-mono placeholder-[#4a4060]"
          />
        </div>
        <button
          onClick={() => setPaused(!paused)}
          className={`flex items-center justify-center gap-1.5 rounded-lg px-4 py-2 text-sm font-medium border transition-colors ${paused ? 'border-yellow-600 text-yellow-500 bg-yellow-950/30' : 'border-[#2a2240] text-[#D0CFCC] bg-[#171421]'} hover:border-[#D0CFCC] whitespace-nowrap`}
        >
          {paused ? <Play size={16} /> : <Pause size={16} />}
          <span className="hidden sm:inline">{paused ? 'Resume' : 'Pause'}</span>
        </button>
        <span className={`w-2.5 h-2.5 rounded-full shrink-0 ${connected ? 'bg-[#D0CFCC]' : 'bg-red-500'}`} />
      </div>

      {/* Log Output */}
      <div
        ref={containerRef}
        onScroll={handleScroll}
        className="flex-1 overflow-y-auto px-2 sm:px-3 py-2 font-mono text-[13px] sm:text-sm leading-relaxed"
      >
        {filtered.length === 0 && (
          <div className="text-[#4a4060] animate-pulse text-[13px] sm:text-sm">Waiting for logs...</div>
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
