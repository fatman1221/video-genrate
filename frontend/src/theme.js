import { alpha, createTheme } from '@mui/material/styles'

/** 主色：克制的紫。深色用于强调，soft 用于浅底。 */
export const PURPLE = {
  main: '#6D4AFF',
  light: '#9B82FF',
  dark: '#4A2FCC',
  soft: '#F1EDFF',
  ink: '#1A1726',
}

/** 深色模式下的主色：提亮一档，保证在暗底上仍有足够对比。 */
export const PURPLE_DARK = {
  main: '#9B82FF',
  light: '#B9A6FF',
  dark: '#7C5CFF',
  soft: 'rgba(155,130,255,0.16)',
}

export const MODES = ['light', 'dark']

/**
 * 生成主题。浅色是默认；暗色沿用同一套主色，只换底色与文字色。
 *
 * 之所以做成函数而不是导出一个常量：深色模式需要整体重建主题，
 * 且 MUI 的 `alpha()` 依赖当前 palette 的文字色，必须按模式分别计算。
 */
export function createAppTheme(mode = 'light') {
  const dark = mode === 'dark'
  const primary = dark ? PURPLE_DARK : PURPLE
  const ink = dark ? '#EDEBF7' : PURPLE.ink
  const muted = dark ? '#A6A2B8' : '#6E6A80'
  // 描边统一走 divider，组件里不再硬编码半透明黑，深浅两套自动适配
  const hairlineColor = dark ? 'rgba(237,235,247,0.10)' : 'rgba(26,23,38,0.07)'
  const HAIRLINE = `1px solid ${hairlineColor}`

  return createTheme({
    palette: {
      mode,
      primary: {
        main: primary.main,
        light: primary.light,
        dark: primary.dark,
        contrastText: '#fff',
      },
      secondary: { main: dark ? '#3ED3C4' : '#12B5A6' },
      success: { main: dark ? '#3DC98D' : '#12A56A' },
      warning: { main: dark ? '#E9A44E' : '#E0902B' },
      error: { main: dark ? '#F0676B' : '#E2484C' },
      info: { main: dark ? '#6FA6F7' : '#4C8DF5' },
      background: {
        default: dark ? '#0F0E17' : '#F7F7FB',
        paper: dark ? '#191826' : '#FFFFFF',
      },
      text: { primary: ink, secondary: muted, disabled: dark ? '#6B6880' : '#A9A5B8' },
      divider: hairlineColor,
      action: {
        hover: dark ? 'rgba(237,235,247,0.06)' : 'rgba(26,23,38,0.035)',
        selected: dark ? 'rgba(155,130,255,0.16)' : 'rgba(109,74,255,0.09)',
      },
    },
    shape: { borderRadius: 12 },
    typography: {
      fontFamily:
        '-apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", Inter, Roboto, "Helvetica Neue", Arial, sans-serif',
      h3: { fontWeight: 700, letterSpacing: '-1px' },
      h4: { fontWeight: 700, letterSpacing: '-0.8px' },
      h5: { fontWeight: 700, letterSpacing: '-0.5px' },
      h6: { fontWeight: 650, letterSpacing: '-0.3px', fontSize: '1.02rem' },
      subtitle2: { fontWeight: 600 },
      body2: { fontSize: 13.5, lineHeight: 1.7 },
      caption: { fontSize: 11.5, letterSpacing: 0.1 },
      button: { textTransform: 'none', fontWeight: 600, letterSpacing: 0 },
      overline: { letterSpacing: 0.6, fontWeight: 650 },
    },
    components: {
      MuiCssBaseline: {
        styleOverrides: {
          '*::-webkit-scrollbar': { width: 8, height: 8 },
          '*::-webkit-scrollbar-thumb': {
            background: dark ? 'rgba(237,235,247,0.20)' : 'rgba(26,23,38,0.14)',
            borderRadius: 8,
          },
          '*::-webkit-scrollbar-track': { background: 'transparent' },
          body: { WebkitFontSmoothing: 'antialiased' },
          // 视频/音频控件在暗底上保持可读
          '::selection': { background: alpha(primary.main, 0.28) },
        },
      },
      MuiPaper: {
        defaultProps: { elevation: 0 },
        styleOverrides: { root: { backgroundImage: 'none' } },
      },
      MuiCard: {
        defaultProps: { elevation: 0 },
        styleOverrides: {
          root: {
            border: HAIRLINE,
            borderRadius: 16,
            boxShadow: dark
              ? '0 1px 2px rgba(0,0,0,0.3), 0 18px 40px -30px rgba(0,0,0,0.8)'
              : '0 1px 2px rgba(26,23,38,0.03), 0 18px 40px -28px rgba(74,47,204,0.28)',
          },
        },
      },
      MuiAppBar: {
        defaultProps: { elevation: 0, color: 'transparent' },
        styleOverrides: {
          root: {
            background: dark ? 'rgba(15,14,23,0.78)' : 'rgba(255,255,255,0.82)',
            backdropFilter: 'saturate(180%) blur(14px)',
            borderBottom: HAIRLINE,
            color: ink,
          },
        },
      },
      MuiButton: {
        defaultProps: { disableElevation: true },
        styleOverrides: {
          root: { borderRadius: 10, paddingInline: 14 },
          containedPrimary: {
            boxShadow: dark
              ? '0 8px 20px -12px rgba(155,130,255,0.9)'
              : '0 8px 20px -10px rgba(109,74,255,0.7)',
          },
        },
      },
      MuiIconButton: { styleOverrides: { root: { borderRadius: 10 } } },
      MuiTab: {
        styleOverrides: {
          root: {
            textTransform: 'none',
            fontWeight: 600,
            minHeight: 44,
            minWidth: 'auto',
            paddingInline: 12,
            color: muted,
            '&.Mui-selected': { color: primary.main },
          },
        },
      },
      MuiTabs: { styleOverrides: { indicator: { height: 2, borderRadius: 2 } } },
      MuiChip: {
        styleOverrides: {
          root: { fontWeight: 600, borderRadius: 8 },
          sizeSmall: { height: 22, fontSize: 11.5 },
        },
      },
      MuiLinearProgress: {
        styleOverrides: {
          root: {
            borderRadius: 999,
            height: 6,
            backgroundColor: dark ? 'rgba(237,235,247,0.10)' : 'rgba(26,23,38,0.06)',
          },
          bar: { borderRadius: 999 },
        },
      },
      MuiTableCell: {
        styleOverrides: {
          head: { fontWeight: 650, color: muted, fontSize: 12, borderBottom: HAIRLINE },
          root: {
            borderBottom: `1px solid ${hairlineColor}`,
            fontSize: 13,
          },
        },
      },
      MuiDialog: { styleOverrides: { paper: { borderRadius: 18, border: HAIRLINE } } },
      MuiTooltip: {
        styleOverrides: {
          tooltip: {
            backgroundColor: dark ? '#2A2840' : '#241F35',
            fontSize: 12,
            borderRadius: 8,
            padding: '6px 10px',
          },
        },
      },
      MuiDrawer: { styleOverrides: { paper: { borderLeft: HAIRLINE, backgroundImage: 'none' } } },
      MuiToggleButton: {
        styleOverrides: {
          root: {
            textTransform: 'none',
            fontWeight: 600,
            borderRadius: 10,
            border: HAIRLINE,
            color: muted,
            paddingBlock: 4,
            '&.Mui-selected': {
              backgroundColor: dark ? 'rgba(155,130,255,0.18)' : PURPLE.soft,
              color: primary.main,
              '&:hover': { backgroundColor: dark ? 'rgba(155,130,255,0.24)' : PURPLE.soft },
            },
          },
        },
      },
      MuiTextField: { defaultProps: { size: 'small' } },
      MuiSlider: {
        styleOverrides: {
          rail: { backgroundColor: dark ? 'rgba(237,235,247,0.16)' : 'rgba(26,23,38,0.10)' },
        },
      },
      MuiSkeleton: {
        styleOverrides: {
          root: { backgroundColor: dark ? 'rgba(237,235,247,0.08)' : 'rgba(26,23,38,0.06)' },
        },
      },
    },
  })
}

/** 默认导出浅色主题，保持旧引用可用。 */
const theme = createAppTheme('light')

export default theme
