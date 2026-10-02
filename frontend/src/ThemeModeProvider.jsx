import { createContext, useContext, useEffect, useMemo, useState } from 'react'
import { CssBaseline, ThemeProvider } from '@mui/material'
import { createAppTheme } from './theme'

const STORAGE_KEY = 'avs.theme-mode'

const ThemeModeContext = createContext({ mode: 'light', setMode: () => {}, toggle: () => {} })

/** 读/写当前主题模式（App 顶栏的切换按钮用它）。 */
export function useThemeMode() {
  return useContext(ThemeModeContext)
}

function initialMode() {
  try {
    const saved = localStorage.getItem(STORAGE_KEY)
    if (saved === 'light' || saved === 'dark') return saved
  } catch {
    /* 隐私模式下 localStorage 可能不可用 */
  }
  return window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
}

export default function ThemeModeProvider({ children }) {
  const [mode, setMode] = useState(initialMode)

  useEffect(() => {
    try {
      localStorage.setItem(STORAGE_KEY, mode)
    } catch {
      /* 忽略写入失败，不影响使用 */
    }
  }, [mode])

  const theme = useMemo(() => createAppTheme(mode), [mode])
  const value = useMemo(
    () => ({
      mode,
      setMode,
      toggle: () => setMode((m) => (m === 'dark' ? 'light' : 'dark')),
    }),
    [mode],
  )

  return (
    <ThemeModeContext.Provider value={value}>
      <ThemeProvider theme={theme}>
        <CssBaseline />
        {children}
      </ThemeProvider>
    </ThemeModeContext.Provider>
  )
}
