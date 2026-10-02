import { Fragment, useState } from 'react'
import {
  Box,
  Button,
  Chip,
  Collapse,
  IconButton,
  LinearProgress,
  Paper,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  Tooltip,
  Typography,
} from '@mui/material'
import ExpandMoreIcon from '@mui/icons-material/ExpandMore'
import ExpandLessIcon from '@mui/icons-material/ExpandLess'
import ReplayIcon from '@mui/icons-material/Replay'
import CancelIcon from '@mui/icons-material/Cancel'
import { fmtTime, statusColor } from '../api'

export default function TaskTable({ tasks = [], onRetry, onCancel }) {
  const [expanded, setExpanded] = useState(null)

  if (!tasks.length) {
    return (
      <Typography color="text.secondary" sx={{ py: 4, textAlign: 'center' }}>
        还没有任务。点击上方「推进流水线」提交生成任务。
      </Typography>
    )
  }

  return (
    <Paper>
      <Table size="small">
        <TableHead>
          <TableRow>
            <TableCell />
            <TableCell>任务</TableCell>
            <TableCell>类型</TableCell>
            <TableCell>状态</TableCell>
            <TableCell sx={{ width: 170 }}>进度</TableCell>
            <TableCell align="right">尝试</TableCell>
            <TableCell>创建时间</TableCell>
            <TableCell align="right">操作</TableCell>
          </TableRow>
        </TableHead>
        <TableBody>
          {tasks.map((t) => (
            <Fragment key={t.task_id}>
              <TableRow hover>
                <TableCell padding="checkbox">
                  <IconButton size="small" onClick={() => setExpanded(expanded === t.task_id ? null : t.task_id)}>
                    {expanded === t.task_id ? <ExpandLessIcon /> : <ExpandMoreIcon />}
                  </IconButton>
                </TableCell>
                <TableCell>
                  <Typography variant="body2" fontWeight={600}>
                    {t.name}
                  </Typography>
                  <Typography variant="caption" color="text.secondary">
                    {t.task_id}
                    {t.shot_id ? ` · ${t.shot_id}` : ''}
                  </Typography>
                </TableCell>
                <TableCell>
                  <Chip size="small" variant="outlined" label={t.type} />
                </TableCell>
                <TableCell>
                  <Chip size="small" color={statusColor(t.status)} label={t.status} />
                </TableCell>
                <TableCell>
                  <Stack direction="row" spacing={1} alignItems="center">
                    <LinearProgress variant="determinate" value={t.progress || 0} sx={{ flex: 1 }} />
                    <Typography variant="caption">{t.progress || 0}%</Typography>
                  </Stack>
                </TableCell>
                <TableCell align="right">
                  {t.attempts}/{t.max_attempts}
                </TableCell>
                <TableCell>{fmtTime(t.created_at)}</TableCell>
                <TableCell align="right">
                  <Tooltip title="重新入队">
                    <IconButton size="small" onClick={() => onRetry(t)}>
                      <ReplayIcon fontSize="small" />
                    </IconButton>
                  </Tooltip>
                  <Tooltip title="取消">
                    <span>
                      <IconButton
                        size="small"
                        color="error"
                        disabled={['SUCCESS', 'FAILED', 'CANCELLED'].includes(t.status)}
                        onClick={() => onCancel(t)}
                      >
                        <CancelIcon fontSize="small" />
                      </IconButton>
                    </span>
                  </Tooltip>
                </TableCell>
              </TableRow>
              <TableRow key={`${t.task_id}-detail`}>                <TableCell colSpan={8} sx={{ py: 0, borderBottom: expanded === t.task_id ? undefined : 'none' }}>
                  <Collapse in={expanded === t.task_id} timeout="auto" unmountOnExit>
                    <Box sx={{ py: 2, px: 1 }}>
                      {t.error && (
                        <Typography variant="caption" color="error" sx={{ display: 'block', mb: 1 }}>
                          错误：{t.error}
                        </Typography>
                      )}
                      <Stack direction={{ xs: 'column', md: 'row' }} spacing={2}>
                        <Box sx={{ flex: 1 }}>
                          <Typography variant="caption" color="text.secondary">
                            执行日志
                          </Typography>
                          <Box
                            sx={{
                              mt: 0.5,
                              bgcolor: '#1b1726',
                              color: '#d9d4ea',
                              p: 1.2,
                              borderRadius: 1.5,
                              maxHeight: 220,
                              overflow: 'auto',
                              fontFamily: 'monospace',
                              fontSize: 11.5,
                            }}
                          >
                            {(t.logs || []).length === 0 && <div>（暂无日志）</div>}
                            {(t.logs || []).map((log, idx) => (
                              <div key={idx}>
                                <span style={{ opacity: 0.6 }}>{fmtTime(log.ts)}</span>{' '}
                                <span style={{ color: log.level === 'ERROR' ? '#ff8a80' : '#c7bfff' }}>
                                  [{log.level}]
                                </span>{' '}
                                {log.message}
                              </div>
                            ))}
                          </Box>
                        </Box>
                        <Box sx={{ flex: 1 }}>
                          <Typography variant="caption" color="text.secondary">
                            结果
                          </Typography>
                          <Box
                            sx={{
                              mt: 0.5,
                              bgcolor: 'action.hover',
                              p: 1.2,
                              borderRadius: 1.5,
                              maxHeight: 220,
                              overflow: 'auto',
                            }}
                          >
                            <pre style={{ margin: 0, fontSize: 11.5 }}>
                              {JSON.stringify(t.result || {}, null, 2)}
                            </pre>
                          </Box>
                          <Typography variant="caption" color="text.secondary" sx={{ mt: 1, display: 'block' }}>
                            提交参数
                          </Typography>
                          <Box
                            sx={{
                              mt: 0.5,
                              bgcolor: 'action.hover',
                              p: 1.2,
                              borderRadius: 1.5,
                              maxHeight: 140,
                              overflow: 'auto',
                            }}
                          >
                            <pre style={{ margin: 0, fontSize: 11.5 }}>
                              {JSON.stringify(t.payload || {}, null, 2)}
                            </pre>
                          </Box>
                        </Box>
                      </Stack>
                    </Box>
                  </Collapse>
                </TableCell>
              </TableRow>
            </Fragment>
          ))}
        </TableBody>
      </Table>
    </Paper>
  )
}
