import MiniSettings from './MiniSettings'

const TG = (window as any).TelegramWebApp ?? (window as any).Telegram?.WebApp
const isTelegram = !!TG?.initData

export default function SettingsView() {
  if (!isTelegram) {
    return (
      <div className="min-h-screen flex flex-col items-center justify-center bg-gray-950 text-gray-400 p-8">
        <h1 className="text-2xl font-bold text-red-400 mb-4">⛔ Access Denied</h1>
        <p className="text-center max-w-md">
          Settings are only available through the Telegram Mini App.
          Open your bot and use <code className="text-green-500 bg-gray-900 px-2 py-0.5 rounded">/settings</code> to access.
        </p>
      </div>
    )
  }

  return <MiniSettings />
}
