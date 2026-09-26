import { createTheme } from '@mui/material/styles'

/** 主色：克制的紫。深色用于强调，soft 用于浅底。 */
export const PURPLE = {
  main: '#6D4AFF',
  light: '#9B82FF',
  dark: '#4A2FCC',
  soft: '#F1EDFF',
  ink: '#1A1726',
}

const HAIRLINE = '1px solid rgba(26, 23, 38, 0.07)'

const theme = createTheme({
  palette: {
    mode: 'light',
    primary: { main: PURPLE.main, light: PURPLE.light, dark: PURPLE.dark, contrastText: '#fff' },
    secondary: { main: '#12B5A6' },
    success: { main: '#12A56A' },
    warning: { main: '#E0902B' },
    error: { main: '#E2484C' },
    info: { main: '#4C8DF5' },
    background: { default: '#F7F7FB', paper: '#FFFFFF' },
    text: { primary: PURPLE.ink, secondary: '#6E6A80', disabled: '#A9A5B8' },
    divider: 'rgba(26, 23, 38, 0.07)',
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
          background: 'rgba(26,23,38,0.14)',
          borderRadius: 8,
        },
        '*::-webkit-scrollbar-track': { background: 'transparent' },
        body: { WebkitFontSmoothing: 'antialiased' },
      },
    },
    MuiPaper: { defaultProps: { elevation: 0 }, styleOverrides: { root: { backgroundImage: 'none' } } },
    MuiCard: {
      defaultProps: { elevation: 0 },
      styleOverrides: {
        root: {
          border: HAIRLINE,
          borderRadius: 16,
          boxShadow: '0 1px 2px rgba(26,23,38,0.03), 0 18px 40px -28px rgba(74,47,204,0.28)',
        },
      },
    },
    MuiAppBar: {
      defaultProps: { elevation: 0, color: 'transparent' },
      styleOverrides: {
        root: {
          background: 'rgba(255,255,255,0.82)',
          backdropFilter: 'saturate(180%) blur(14px)',
          borderBottom: HAIRLINE,
          color: PURPLE.ink,
        },
      },
    },
    MuiButton: {
      defaultProps: { disableElevation: true },
      styleOverrides: {
        root: { borderRadius: 10, paddingInline: 14 },
        containedPrimary: { boxShadow: '0 8px 20px -10px rgba(109,74,255,0.7)' },
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
          color: '#6E6A80',
          '&.Mui-selected': { color: PURPLE.main },
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
        root: { borderRadius: 999, height: 6, backgroundColor: 'rgba(26,23,38,0.06)' },
        bar: { borderRadius: 999 },
      },
    },
    MuiTableCell: {
      styleOverrides: {
        head: { fontWeight: 650, color: '#6E6A80', fontSize: 12, borderBottom: HAIRLINE },
        root: { borderBottom: '1px solid rgba(26,23,38,0.05)', fontSize: 13 },
      },
    },
    MuiDialog: { styleOverrides: { paper: { borderRadius: 18, border: HAIRLINE } } },
    MuiTooltip: {
      styleOverrides: {
        tooltip: { backgroundColor: '#241F35', fontSize: 12, borderRadius: 8, padding: '6px 10px' },
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
          paddingBlock: 4,
          '&.Mui-selected': { backgroundColor: PURPLE.soft, color: PURPLE.dark },
        },
      },
    },
  },
})

export default theme
