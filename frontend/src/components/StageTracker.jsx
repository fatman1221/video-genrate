import { Box, Chip, LinearProgress, Stack, Tooltip, Typography } from '@mui/material'
import CheckCircleIcon from '@mui/icons-material/CheckCircle'
import ErrorIcon from '@mui/icons-material/Error'
import RadioButtonUncheckedIcon from '@mui/icons-material/RadioButtonUnchecked'
import AutorenewIcon from '@mui/icons-material/Autorenew'

const ICON = {
  SUCCESS: { Component: CheckCircleIcon, color: 'success.main' },
  RUNNING: { Component: AutorenewIcon, color: 'primary.main', spin: true },
  FAILED: { Component: ErrorIcon, color: 'error.main' },
  SKIPPED: { Component: RadioButtonUncheckedIcon, color: 'text.disabled' },
  PENDING: { Component: RadioButtonUncheckedIcon, color: 'text.disabled' },
}

export default function StageTracker({ stages = [], progress = 0, workflowState = '' }) {
  return (
    <Box>
      <Stack direction="row" alignItems="center" justifyContent="space-between" sx={{ mb: 1 }}>
        <Typography variant="subtitle2">生产进度</Typography>
        <Stack direction="row" spacing={1} alignItems="center">
          <Chip size="small" variant="outlined" label={workflowState || '—'} color="primary" />
          <Typography variant="h6" color="primary">
            {progress}%
          </Typography>
        </Stack>
      </Stack>
      <LinearProgress variant="determinate" value={progress} sx={{ mb: 2.5, height: 10 }} />

      <Stack direction="row" flexWrap="wrap" gap={1.2}>
        {stages.map((s) => {
          const meta = ICON[s.state] || ICON.PENDING
          const { Component } = meta
          return (
            <Tooltip key={s.step_key} title={s.error || s.detail || s.label}>
              <Box
                sx={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: 0.8,
                  px: 1.2,
                  py: 0.7,
                  borderRadius: 2,
                  border: '1px solid',
                  borderColor: s.state === 'RUNNING' ? 'primary.main' : 'divider',
                  bgcolor: s.state === 'SUCCESS' ? 'rgba(46,158,107,0.07)' : 'background.paper',
                }}
              >
                <Component
                  sx={{
                    fontSize: 18,
                    color: meta.color,
                    animation: meta.spin ? 'spin 1.6s linear infinite' : 'none',
                    '@keyframes spin': { from: { transform: 'rotate(0deg)' }, to: { transform: 'rotate(360deg)' } },
                  }}
                />
                <Box>
                  <Typography variant="body2" fontWeight={600} lineHeight={1.2}>
                    {s.label}
                  </Typography>
                  {s.detail && (
                    <Typography variant="caption" color="text.secondary" lineHeight={1.1}>
                      {s.detail}
                    </Typography>
                  )}
                </Box>
              </Box>
            </Tooltip>
          )
        })}
      </Stack>
    </Box>
  )
}
