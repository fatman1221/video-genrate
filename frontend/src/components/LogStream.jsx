import { Box, Chip, Stack, Typography } from '@mui/material'
import CircleIcon from '@mui/icons-material/Circle'

const LEVEL_COLOR = {
  INFO: '#8C86A8',
  WARN: '#E8A33D',
  ERROR: '#E5484D',
  DEBUG: '#8C86A8',
}

export default function LogStream({ logs = [], connected = false, height = 420 }) {
  return (
    <Box>
      <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 1 }}>
        <Chip
          size="small"
          icon={<CircleIcon sx={{ fontSize: 10 }} />}
          label={connected ? '实时连接中' : '连接已断开'}
          color={connected ? 'success' : 'default'}
          variant="outlined"
        />
        <Typography variant="caption" color="text.secondary">
          共 {logs.length} 条 · SSE 实时推送
        </Typography>
      </Stack>
      <Box
        sx={{
          bgcolor: '#17131f',
          borderRadius: 2,
          p: 1.6,
          height,
          overflow: 'auto',
          fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
          fontSize: 12.5,
          lineHeight: 1.65,
        }}
      >
        {logs.length === 0 && <Box sx={{ color: '#6b6480' }}>暂无日志。执行任意操作后会实时出现这里。</Box>}
        {logs.map((log) => (
          <Box key={log.id} sx={{ display: 'flex', gap: 1, alignItems: 'flex-start', mb: 0.4 }}>
            <Box component="span" sx={{ color: '#6b6480', flexShrink: 0 }}>
              [{new Date(log.ts).toLocaleTimeString('zh-CN', { hour12: false })}]
            </Box>
            <Box
              component="span"
              sx={{ color: LEVEL_COLOR[log.level] || '#8C86A8', flexShrink: 0, minWidth: 52 }}
            >
              {log.actor}
            </Box>
            <Box component="span" sx={{ color: log.level === 'ERROR' ? '#ff9a9a' : '#e7e3f5' }}>
              {log.message}
            </Box>
          </Box>
        ))}
      </Box>
    </Box>
  )
}
