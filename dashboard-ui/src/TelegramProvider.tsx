import { useEffect, type ReactNode } from 'react'

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
  }, [])

  return <>{children}</>
}

export { TG }
