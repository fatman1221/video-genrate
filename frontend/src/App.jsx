import { AppBar, Box, IconButton, Link, Stack, Toolbar, Tooltip, Typography } from '@mui/material'
import MovieFilterIcon from '@mui/icons-material/MovieFilter'
import TerminalOutlinedIcon from '@mui/icons-material/TerminalOutlined'
import MenuBookOutlinedIcon from '@mui/icons-material/MenuBookOutlined'
import Brightness4Icon from '@mui/icons-material/Brightness4'
import Brightness7Icon from '@mui/icons-material/Brightness7'
import { Link as RouterLink, NavLink, Route, Routes } from 'react-router-dom'
import ProjectList from './pages/ProjectList'
import ProjectDetail from './pages/ProjectDetail'
import SeriesList from './pages/SeriesList'
import SeriesDetail from './pages/SeriesDetail'
import AssetCenter from './pages/AssetCenter'
import Settings from './pages/Settings'
import { useThemeMode } from './ThemeModeProvider'
import { useApi } from './api'

function NavItem({ to, label }) {
  return (
    <Box
      component={NavLink}
      to={to}
      end
      sx={{
        px: 1.4,
        py: 0.6,
        borderRadius: 2,
        fontSize: 13.5,
        fontWeight: 600,
        textDecoration: 'none',
        color: 'text.secondary',
        transition: 'all .15s',
        '&.active': { color: 'primary.main', bgcolor: 'action.selected' },
        '&:hover': { color: 'text.primary' },
      }}
    >
      {label}
    </Box>
  )
}

export default function App() {
  const { data } = useApi('/api/health', null, { poll: 20000 })
  const { mode, toggle } = useThemeMode()
  const ok = data?.status === 'ok'
  const dark = mode === 'dark'

  return (
    <Box sx={{ minHeight: '100vh', bgcolor: 'background.default' }}>
      <AppBar position="sticky">
        <Toolbar sx={{ gap: 1.3, minHeight: 58 }}>
          <Box
            sx={{
              width: 30, height: 30, borderRadius: 9, bgcolor: 'primary.main', color: '#fff',
              display: 'flex', alignItems: 'center', justifyContent: 'center',
              boxShadow: `0 6px 16px -8px ${dark ? 'rgba(155,130,255,0.9)' : 'rgba(109,74,255,0.9)'}`,
            }}
          >
            <MovieFilterIcon sx={{ fontSize: 17 }} />
          </Box>
          <Link
            component={RouterLink}
            to="/"
            sx={{ color: 'text.primary', textDecoration: 'none', fontWeight: 700, fontSize: 15.5, letterSpacing: '-0.3px' }}
          >
            AI Video Studio
          </Link>
          <Typography variant="caption" color="text.disabled" sx={{ ml: 0.4, display: { xs: 'none', sm: 'block' } }}>
            Agent 工具箱 · Skill Server · 执行平台
          </Typography>

          <Stack direction="row" spacing={0.4} sx={{ ml: 2 }}>
            <NavItem to="/" label="项目" />
            <NavItem to="/series" label="连续剧" />
            <NavItem to="/assets" label="素材中心" />
            <NavItem to="/settings" label="系统设置" />
          </Stack>

          <Box sx={{ flex: 1 }} />

          <Tooltip title={ok ? '后端 / 数据库 / ffmpeg 就绪' : '后端异常'}>
            <Box
              sx={{
                display: 'flex', alignItems: 'center', gap: 0.7, px: 1.2, py: 0.5,
                borderRadius: 999, border: '1px solid', borderColor: 'divider',
                bgcolor: 'action.hover',
              }}
            >
              <Box
                sx={{
                  width: 7, height: 7, borderRadius: '50%',
                  bgcolor: ok ? 'success.main' : 'error.main',
                  boxShadow: ok ? '0 0 0 3px rgba(18,165,106,0.14)' : '0 0 0 3px rgba(226,72,76,0.14)',
                }}
              />
              <Typography variant="caption" color="text.secondary">
                {ok ? `${data?.workers?.count ?? 0} worker 就绪` : '引擎离线'}
              </Typography>
            </Box>
          </Tooltip>

          <Tooltip title="Skill 清单（Agent 能力契约）">
            <IconButton size="small" href="/api/skills" target="_blank">
              <TerminalOutlinedIcon fontSize="small" />
            </IconButton>
          </Tooltip>
          <Tooltip title="API 文档">
            <IconButton size="small" href="/docs" target="_blank">
              <MenuBookOutlinedIcon fontSize="small" />
            </IconButton>
          </Tooltip>
          <Tooltip title={dark ? '切换到浅色模式' : '切换到深色模式'}>
            <IconButton size="small" onClick={toggle}>
              {dark ? <Brightness7Icon fontSize="small" /> : <Brightness4Icon fontSize="small" />}
            </IconButton>
          </Tooltip>
        </Toolbar>
      </AppBar>

      <Box sx={{ maxWidth: 1500, mx: 'auto', px: { xs: 2, md: 3 }, py: 3 }}>
        <Routes>
          <Route path="/" element={<ProjectList />} />
          <Route path="/projects/:projectId" element={<ProjectDetail />} />
          <Route path="/series" element={<SeriesList />} />
          <Route path="/series/:seriesId" element={<SeriesDetail />} />
          <Route path="/assets" element={<AssetCenter />} />
          <Route path="/settings" element={<Settings />} />
        </Routes>
      </Box>
    </Box>
  )
}
