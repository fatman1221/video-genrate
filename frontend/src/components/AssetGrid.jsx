import { useState } from 'react'
import {
  Box,
  Button,
  Chip,
  Dialog,
  DialogContent,
  DialogTitle,
  Divider,
  Grid,
  IconButton,
  Stack,
  Tooltip,
  Typography,
} from '@mui/material'
import DownloadIcon from '@mui/icons-material/Download'
import DeleteOutlineIcon from '@mui/icons-material/DeleteOutline'
import RefreshIcon from '@mui/icons-material/Refresh'
import { fmtBytes, fmtDuration, fmtTime, statusColor } from '../api'

function Preview({ asset }) {
  if (!asset?.url) return null
  if (asset.type === 'VIDEO' || asset.format === 'mp4') {
    return <Box component="video" src={asset.url} controls sx={{ width: '100%', borderRadius: 2, bgcolor: '#000' }} />
  }
  if (['VOICE', 'MUSIC', 'SFX'].includes(asset.type)) {
    return <Box component="audio" src={asset.url} controls sx={{ width: '100%' }} />
  }
  if (['IMAGE', 'CHARACTER', 'SCENE'].includes(asset.type)) {
    return (
      <Box
        component="img"
        src={asset.url}
        alt={asset.name}
        sx={{ width: '100%', borderRadius: 2, bgcolor: 'action.hover' }}
      />
    )
  }
  return (
    <Box sx={{ p: 2, bgcolor: '#1b1726', color: '#e9e6f5', borderRadius: 2, maxHeight: 320, overflow: 'auto' }}>
      <pre style={{ margin: 0, whiteSpace: 'pre-wrap', fontSize: 12 }}>
        {asset.extra?.text_preview || '字幕文件，可下载查看（SRT / ASS）'}
      </pre>
    </Box>
  )
}

export default function AssetGrid({ assets = [], onDelete, onRegenerate, busy, showProject = false }) {
  const [detail, setDetail] = useState(null)

  if (!assets.length) {
    return (
      <Typography color="text.secondary" sx={{ py: 4, textAlign: 'center' }}>
        该分类下暂无素材
      </Typography>
    )
  }

  return (
    <>
      <Grid container spacing={2}>
        {assets.map((a) => (
          <Grid item xs={12} sm={6} md={4} lg={3} key={a.asset_id}>
            <Box
              sx={{
                border: '1px solid',
                borderColor: 'divider',
                borderRadius: 3,
                overflow: 'hidden',
                bgcolor: 'background.paper',
                display: 'flex',
                flexDirection: 'column',
                height: '100%',
              }}
            >
              <Box
                onClick={() => setDetail(a)}
                sx={{ cursor: 'pointer', bgcolor: '#f6f4fc', minHeight: 120, display: 'flex', alignItems: 'center' }}
              >
                {['IMAGE', 'CHARACTER', 'SCENE'].includes(a.type) ? (
                  <Box component="img" src={a.url} alt={a.name} sx={{ width: '100%', aspectRatio: '16/9', objectFit: 'cover' }} />
                ) : a.type === 'VIDEO' ? (
                  <Box
                    component="video"
                    src={a.url}
                    preload="metadata"
                    muted
                    sx={{ width: '100%', aspectRatio: '16/9', objectFit: 'cover' }}
                  />
                ) : (
                  <Box sx={{ p: 2, width: '100%', textAlign: 'center' }}>
                    <Typography variant="h4" color="primary.light">
                      ♪
                    </Typography>
                    <Typography variant="caption" color="text.secondary">
                      {a.format.toUpperCase()} · {fmtDuration(a.duration)}
                    </Typography>
                  </Box>
                )}
              </Box>
              <Box sx={{ p: 1.5, flex: 1, display: 'flex', flexDirection: 'column' }}>
                <Stack direction="row" spacing={0.6} alignItems="center" sx={{ mb: 0.5, flexWrap: 'wrap', gap: 0.6 }}>
                  <Chip size="small" label={a.type} color="primary" variant="outlined" />
                  <Chip size="small" label={a.status} color={statusColor(a.status)} />
                  {showProject && (
                    <Chip
                      size="small"
                      variant="outlined"
                      color={a.project_id ? 'default' : 'secondary'}
                      label={a.project_id ? a.project_id.slice(-6) : '独立素材'}
                    />
                  )}
                </Stack>
                <Typography variant="body2" fontWeight={600} noWrap title={a.name}>
                  {a.name}
                </Typography>
                <Typography variant="caption" color="text.secondary">
                  {a.provider} · {fmtBytes(a.size_bytes)}
                  {a.width ? ` · ${a.width}x${a.height}` : ''}
                </Typography>
                <Typography variant="caption" color="text.secondary" noWrap>
                  {fmtTime(a.created_at)}
                </Typography>
                <Stack direction="row" spacing={0.5} sx={{ mt: 1 }}>
                  <Tooltip title="下载">
                    <IconButton size="small" href={`/api/assets/${a.asset_id}/download`} target="_blank">
                      <DownloadIcon fontSize="small" />
                    </IconButton>
                  </Tooltip>
                  {a.shot_id && onRegenerate && (
                    <Tooltip title="重新生成">
                      <span>
                        <IconButton size="small" disabled={busy} onClick={() => onRegenerate(a)}>
                          <RefreshIcon fontSize="small" />
                        </IconButton>
                      </span>
                    </Tooltip>
                  )}
                  <Tooltip title="删除">
                    <IconButton size="small" color="error" onClick={() => onDelete(a)}>
                      <DeleteOutlineIcon fontSize="small" />
                    </IconButton>
                  </Tooltip>
                </Stack>
              </Box>
            </Box>
          </Grid>
        ))}
      </Grid>

      <Dialog open={Boolean(detail)} onClose={() => setDetail(null)} maxWidth="md" fullWidth>
        <DialogTitle>
          {detail?.name}
          <Typography variant="caption" color="text.secondary" sx={{ ml: 1 }}>
            {detail?.asset_id}
          </Typography>
        </DialogTitle>
        <DialogContent dividers>
          {detail && (
            <Grid container spacing={2}>
              <Grid item xs={12} md={6}>
                <Preview asset={detail} />
              </Grid>
              <Grid item xs={12} md={6}>
                <Stack spacing={1}>
                  {[
                    ['类型', detail.type],
                    ['状态', detail.status],
                    ['Provider', detail.provider],
                    ['Model', detail.model],
                    ['Workflow', detail.workflow],
                    ['来源', detail.source],
                    ['尺寸', detail.width ? `${detail.width}x${detail.height}` : '—'],
                    ['时长', fmtDuration(detail.duration)],
                    ['帧率', detail.fps || '—'],
                    ['大小', fmtBytes(detail.size_bytes)],
                    ...(showProject
                      ? [['归属项目', detail.project_id || '独立素材（不属于任何项目）']]
                      : []),
                    ['关联镜头', detail.shot_id || '—'],
                    ['关联角色', detail.character_id || '—'],
                    ['创建时间', fmtTime(detail.created_at)],
                  ].map(([k, v]) => (
                    <Stack direction="row" key={k} spacing={1}>
                      <Typography variant="caption" color="text.secondary" sx={{ minWidth: 76 }}>
                        {k}
                      </Typography>
                      <Typography variant="caption" sx={{ wordBreak: 'break-all' }}>
                        {String(v)}
                      </Typography>
                    </Stack>
                  ))}
                  <Divider />
                  <Typography variant="caption" color="text.secondary">
                    Prompt
                  </Typography>
                  <Typography variant="caption" sx={{ whiteSpace: 'pre-wrap' }}>
                    {detail.prompt || '—'}
                  </Typography>
                  <Typography variant="caption" color="text.secondary">
                    生成参数
                  </Typography>
                  <Box sx={{ bgcolor: 'action.hover', p: 1, borderRadius: 1, maxHeight: 160, overflow: 'auto' }}>
                    <pre style={{ margin: 0, fontSize: 11 }}>
                      {JSON.stringify(detail.parameters || {}, null, 2)}
                    </pre>
                  </Box>
                  <Stack direction="row" spacing={1}>
                    <Button size="small" variant="contained" href={detail.url} target="_blank">
                      打开原文件
                    </Button>
                    <Button size="small" variant="outlined" href={`/api/assets/${detail.asset_id}/download`}>
                      下载
                    </Button>
                  </Stack>
                </Stack>
              </Grid>
            </Grid>
          )}
        </DialogContent>
      </Dialog>
    </>
  )
}
