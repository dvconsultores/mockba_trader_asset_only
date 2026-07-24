import { useEffect, useState, type ReactNode } from 'react'

interface TelegramWebApp {
  ready: () => void
  expand: () => void
  enableClosingConfirmation: () => void
  disableVerticalSwipes: () => void
  BackButton: {
    isVisible: boolean
    show: () => void
    hide: () => void
    onClick: (cb: () => void) => void
    offClick: (cb: () => void) => void
  }
  isVerticalSwipesEnabled?: boolean
  initData?: string
  platform?: string
}

const TG = (window as any).TelegramWebApp ?? (window as any).Telegram?.WebApp as TelegramWebApp | undefined
export const isTelegram = !!TG?.initData

export function TelegramProvider({ children }: { children: ReactNode }) {
  const [ready, setReady] = useState(false)

  useEffect(() => {
    if (!TG) return

    TG.ready()
    TG.expand()

    // Prevent Telegram's pull-down-to-close and scroll-to-minimize gestures
    try {
      TG.disableVerticalSwipes?.()
      TG.enableClosingConfirmation?.()
    } catch {
      // Older clients may not support these methods
    }

    // Small delay to ensure WebApp is fully initialized before showing back button
    setTimeout(() => setReady(true), 50)
  }, [])

  // Show back button by default (Telegram native close on home tab)
  useEffect(() => {
    if (!ready || !TG?.BackButton) return
    TG.BackButton.show()
  }, [ready])

  return <>{children}</>
}

export { TG }
export function useTelegramReady() {
  // Simple hook so App.tsx can wait for Telegram init
  const [tgReady, setTgReady] = useState(isTelegram)
  useEffect(() => {
    if (isTelegram) {
      setTgReady(true)
    }
  }, [])
  return tgReady
}
