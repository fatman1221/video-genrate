import { Box, Stack, ToggleButton, ToggleButtonGroup, Typography } from '@mui/material'
import SubscriptionsOutlinedIcon from '@mui/icons-material/SubscriptionsOutlined'
import ViewQuiltIcon from '@mui/icons-material/ViewQuilt'
import { useNavigate } from 'react-router-dom'
import ProjectList from './ProjectList'
import SeriesList from './SeriesList'

const HINT = {
  series: '一部剧 → 多集 → 每集一条完整流水线 · 角色形象跨集复用',
  project: '每个单集都是一条独立的完整流水线 · 不属于任何剧的节目也在这里',
}

export default function Workspace({ view = 'series' }) {
  const navigate = useNavigate()
  const isSeries = view !== 'project'

  return (
    <Box>
      <Stack
        direction={{ xs: 'column', sm: 'row' }}
        spacing={2}
        alignItems={{ sm: 'flex-end' }}
        sx={{ mb: 3 }}
      >
        <Box sx={{ flex: 1 }}>
          <Typography variant="h3" sx={{ fontSize: 30 }}>
            工作台
          </Typography>
          <Typography variant="body2" color="text.secondary" sx={{ mt: 0.8 }}>
            {isSeries ? HINT.series : HINT.project}
          </Typography>
        </Box>
        <ToggleButtonGroup
          exclusive
          size="small"
          value={isSeries ? 'series' : 'project'}
          onChange={(_, v) => v && navigate(v === 'series' ? '/' : '/projects')}
          sx={{ bgcolor: 'action.hover', p: 0.4, borderRadius: 2.5 }}
        >
          <ToggleButton
            value="series"
            sx={{ px: 1.8, gap: 0.7, border: 0, fontWeight: 600, borderRadius: '8px !important' }}
          >
            <SubscriptionsOutlinedIcon sx={{ fontSize: 17 }} />
            连续剧
          </ToggleButton>
          <ToggleButton
            value="project"
            sx={{ px: 1.8, gap: 0.7, border: 0, fontWeight: 600, borderRadius: '8px !important' }}
          >
            <ViewQuiltIcon sx={{ fontSize: 17 }} />
            单集
          </ToggleButton>
        </ToggleButtonGroup>
      </Stack>

      {isSeries ? <SeriesList /> : <ProjectList />}
    </Box>
  )
}
