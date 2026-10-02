import { useEffect, useMemo, useState } from 'react'
import {
  Alert,
  Box,
  Button,
  Card,
  CardContent,
  Chip,
  Divider,
  Grid,
  IconButton,
  Stack,
  Tooltip,
  Typography,
} from '@mui/material'
import DownloadIcon from '@mui/icons-material/Download'
import PlayArrowIcon from '@mui/icons-material/PlayArrow'
import CheckCircleIcon from '@mui/icons-material/CheckCircle'
import ErrorIcon from '@mui/icons-material/Error'
import WarningAmberIcon from '@mui/icons-material/WarningAmber'
import CompareIcon from '@mui/icons-material/Compare'
import OpenInNewIcon from '@mui/icons-material/OpenInNew'
import RefreshIcon from '@mui/icons-material/Refresh'
import MovieCreationIcon from '@mui/icons-material/MovieCreation'
import { fmtBytes, fmtDuration, fmtTime, getProjectOutputs } from '../api'

const CHECK_ICON = {
  PASS: { Icon: CheckCircleIcon, color: 'success.main' },
  FAIL: { Icon: ErrorIcon, color: 'error.main' },
  WARN: { Icon: WarningAmberIcon, color: 'warning.main' },
}

/**
 * 成片预览与版本对比。
 *
 * 每次「合成」都会在 `storage/outputs/{project_id}/` 落一个新文件，
 * 所以这里直接列目录拿全部历史版本：主播放器放当前选中的一版，
 * 右侧列表可切换预览、一键下载；打开对比模式则把两版并排放。
 */
export default function FinalPanel({ projectId, asset, quality, busy, onCompose, onQuality, onRefresh }) {
  const [versions, setVersions] = useState([])
  const [selectedName, setSelectedName] = useState('')
  const [compareName, setCompareName] = useState('')
  const [loading, setLoading] = useState(false)

  const loadVersions = async () => {
    if (!projectId) return
    setLoading(true)
    try {
      const data = await getProjectOutputs(projectId)
      const items = data?.items || []
      setVersions(items)
      // 默认选中「当前生效」的那一版，没有则取最新
      setSelectedName((prev) => {
        if (prev && items.some((v) => v.name === prev)) return prev
        return (items.find((v) => v.is_current) || items[0])?.name || ''
      })
      return items
    } catch {
      setVersions([])
      return []
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    loadVersions()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId, asset?.url])

  const selected = useMemo(
    () => versions.find((v) => v.name === selectedName) || null,
    [versions, selectedName],
  )
  const compared = useMemo(
    () => versions.find((v) => v.name === compareName) || null,
    [versions, compareName],
  )

  // 目录为空时回退到 overview 里的成片资产，保证老项目也能看到播放器
  const fallbackUrl = asset?.url || ''
  const mainUrl = selected?.url || fallbackUrl

  const download = (v) => `${v.url}${v.url.includes('?') ? '&' : '?'}download=1`

  return (
    <Grid container spacing={2.5}>
      <Grid item xs={12} lg={7}>
        <Card>
          <CardContent>
            <Stack direction="row" alignItems="center" justifyContent="space-between" sx={{ mb: 1.5 }}>
              <Stack direction="row" spacing={1} alignItems="center">
                <Typography variant="h6">Final Video</Typography>
                {selected?.is_current && <Chip size="small" color="success" label="当前生效" />}
              </Stack>
              <Stack direction="row" spacing={0.5}>
                <Tooltip title="刷新版本列表">
                  <span>
                    <IconButton size="small" onClick={loadVersions} disabled={loading}>
                      <RefreshIcon fontSize="small" />
                    </IconButton>
                  </span>
                </Tooltip>
              </Stack>
            </Stack>

            {mainUrl ? (
              <>
                {compared ? (
                  // 并排对比：两个 16:9 播放器
                  <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1.5}>
                    {[
                      { v: selected, tag: 'A', tone: 'primary' },
                      { v: compared, tag: 'B', tone: 'secondary' },
                    ].map(({ v, tag, tone }) => (
                      <Box key={tag} sx={{ flex: 1, minWidth: 0 }}>
                        <Stack direction="row" spacing={0.8} alignItems="center" sx={{ mb: 0.6 }}>
                          <Chip size="small" color={tone} label={tag} />
                          <Typography variant="caption" color="text.secondary" noWrap>
                            {v ? `${fmtTime(v.created_at)} · ${fmtBytes(v.size_bytes)}` : '—'}
                          </Typography>
                        </Stack>
                        <Box
                          component="video"
                          src={v?.url}
                          controls
                          sx={{ width: '100%', borderRadius: 2, bgcolor: '#000', aspectRatio: '16/9' }}
                        />
                      </Box>
                    ))}
                  </Stack>
                ) : (
                  <Box
                    component="video"
                    src={mainUrl}
                    controls
                    poster={asset?.extra?.poster_url}
                    sx={{ width: '100%', borderRadius: 2, bgcolor: '#000', aspectRatio: '16/9' }}
                  />
                )}

                <Stack direction="row" spacing={2} sx={{ mt: 2 }} flexWrap="wrap">
                  {[
                    ['时长', asset?.duration ? fmtDuration(asset.duration) : '—'],
                    ['分辨率', asset?.width ? `${asset.width}x${asset.height}` : '—'],
                    ['FPS', asset?.fps],
                    ['大小', selected ? fmtBytes(selected.size_bytes) : fmtBytes(asset?.size_bytes)],
                    ['编码', asset?.format?.toUpperCase()],
                    ['生成时间', selected ? fmtTime(selected.created_at) : '—'],
                  ].map(([k, v]) => (
                    <Box key={k}>
                      <Typography variant="caption" color="text.secondary" display="block">
                        {k}
                      </Typography>
                      <Typography variant="body2" fontWeight={600}>
                        {v || '—'}
                      </Typography>
                    </Box>
                  ))}
                </Stack>

                <Stack direction="row" spacing={1.5} sx={{ mt: 2, flexWrap: 'wrap', gap: 1 }}>
                  <Button
                    variant="contained"
                    size="small"
                    startIcon={<PlayArrowIcon />}
                    href={selected?.url || mainUrl}
                    target="_blank"
                  >
                    全屏播放
                  </Button>
                  <Button
                    variant="outlined"
                    size="small"
                    startIcon={<DownloadIcon />}
                    href={selected ? download(selected) : mainUrl}
                    download
                  >
                    下载这一版
                  </Button>
                  <Button
                    variant={compared ? 'contained' : 'outlined'}
                    size="small"
                    color="secondary"
                    startIcon={<CompareIcon />}
                    disabled={versions.length < 2}
                    onClick={() => {
                      if (compared) {
                        setCompareName('')
                        return
                      }
                      const other = versions.find((v) => v.name !== selectedName)
                      setCompareName(other?.name || '')
                    }}
                  >
                    {compared ? '退出对比' : '版本对比'}
                  </Button>
                </Stack>

                {asset?.asset_id && (
                  <Typography variant="caption" color="text.secondary" sx={{ mt: 1.5, display: 'block' }}>
                    {selected?.name || asset.url}
                  </Typography>
                )}
              </>
            ) : (
              <Alert severity="info" icon={false}>
                <Typography variant="body2" sx={{ mb: 1 }}>
                  还没有成片。所有镜头的视频就绪后，点击「合成成片」即可生成带配音、配乐与字幕的最终视频。
                </Typography>
                <Button variant="contained" size="small" disabled={busy} onClick={onCompose}>
                  合成成片
                </Button>
              </Alert>
            )}
          </CardContent>
        </Card>

        {/* 版本列表：每次合成都是一版，可切换预览 / 下载 */}
        {versions.length > 0 && (
          <Card sx={{ mt: 2.5 }}>
            <CardContent>
              <Stack direction="row" alignItems="center" justifyContent="space-between" sx={{ mb: 1.2 }}>
                <Typography variant="h6" sx={{ fontSize: 16 }}>
                  历史版本
                  <Typography component="span" variant="caption" color="text.disabled" sx={{ ml: 1 }}>
                    共 {versions.length} 版 · 点一行切换预览
                  </Typography>
                </Typography>
                <Button size="small" variant="outlined" disabled={busy} onClick={onCompose}>
                  再合成一版
                </Button>
              </Stack>
              <Divider sx={{ mb: 1 }} />
              <Stack spacing={0.8}>
                {versions.map((v) => {
                  const active = v.name === selectedName
                  const isB = v.name === compareName
                  return (
                    <Stack
                      key={v.name}
                      direction="row"
                      spacing={1.2}
                      alignItems="center"
                      onClick={() => setSelectedName(v.name)}
                      sx={{
                        px: 1.2,
                        py: 0.9,
                        borderRadius: 2,
                        cursor: 'pointer',
                        border: '1px solid',
                        borderColor: active ? 'primary.main' : 'divider',
                        bgcolor: active ? 'action.selected' : 'transparent',
                        '&:hover': { bgcolor: 'action.hover' },
                      }}
                    >
                      <Box
                        sx={{
                          width: 8, height: 8, borderRadius: '50%', flexShrink: 0,
                          bgcolor: active ? 'primary.main' : isB ? 'secondary.main' : 'text.disabled',
                        }}
                      />
                      <Box sx={{ flex: 1, minWidth: 0 }}>
                        <Stack direction="row" spacing={0.8} alignItems="center">
                          <Typography variant="body2" fontWeight={600} noWrap>
                            {fmtTime(v.created_at)}
                          </Typography>
                          {v.is_current && <Chip size="small" color="success" variant="outlined" label="生效" />}
                          {isB && <Chip size="small" color="secondary" label="对比 B" />}
                        </Stack>
                        <Typography variant="caption" color="text.disabled" noWrap>
                          {v.name} · {fmtBytes(v.size_bytes)}
                        </Typography>
                      </Box>
                      <Tooltip title="设为对比 B 版">
                        <span>
                          <IconButton
                            size="small"
                            disabled={active || versions.length < 2}
                            onClick={(e) => {
                              e.stopPropagation()
                              setCompareName(isB ? '' : v.name)
                            }}
                          >
                            <CompareIcon fontSize="small" />
                          </IconButton>
                        </span>
                      </Tooltip>
                      <Tooltip title="下载">
                        <IconButton
                          size="small"
                          href={download(v)}
                          download
                          component="a"
                          onClick={(e) => e.stopPropagation()}
                        >
                          <DownloadIcon fontSize="small" />
                        </IconButton>
                      </Tooltip>
                      <Tooltip title="新窗口打开">
                        <IconButton
                          size="small"
                          href={v.url}
                          target="_blank"
                          component="a"
                          onClick={(e) => e.stopPropagation()}
                        >
                          <OpenInNewIcon fontSize="small" />
                        </IconButton>
                      </Tooltip>
                    </Stack>
                  )
                })}
              </Stack>
            </CardContent>
          </Card>
        )}
      </Grid>

      <Grid item xs={12} lg={5}>
        <Card sx={{ height: '100%' }}>
          <CardContent>
            <Stack direction="row" alignItems="center" justifyContent="space-between" sx={{ mb: 1.5 }}>
              <Typography variant="h6">Quality Check</Typography>
              {quality && (
                <Chip
                  label={`${quality.score} 分`}
                  color={quality.status === 'PASS' ? 'success' : quality.status === 'FAIL' ? 'error' : 'warning'}
                />
              )}
            </Stack>

            {!quality && (
              <Box>
                <Typography variant="body2" color="text.secondary">
                  尚未执行质量检查。
                </Typography>
                <Stack direction="row" spacing={1} sx={{ mt: 2 }}>
                  <Button variant="outlined" size="small" disabled={busy} onClick={onQuality}>
                    立即质检
                  </Button>
                  <Button
                    variant="text"
                    size="small"
                    startIcon={<MovieCreationIcon />}
                    disabled={busy}
                    onClick={onCompose}
                  >
                    合成成片
                  </Button>
                </Stack>
              </Box>
            )}

            {quality && (
              <Stack spacing={1}>
                <Typography variant="caption" color="text.secondary">
                  通过 {quality.passed} / {quality.total} · FAIL {quality.failed} · WARN {quality.warned}
                </Typography>
                <Divider />
                {quality.items.map((item) => {
                  const meta = CHECK_ICON[item.status] || CHECK_ICON.WARN
                  const { Icon } = meta
                  return (
                    <Stack key={item.id} direction="row" spacing={1} alignItems="flex-start">
                      <Icon sx={{ fontSize: 18, color: meta.color, mt: 0.3 }} />
                      <Box>
                        <Typography variant="body2" fontWeight={600} lineHeight={1.3}>
                          {item.name}
                        </Typography>
                        {item.message && (
                          <Typography variant="caption" color="text.secondary">
                            {item.message}
                          </Typography>
                        )}
                      </Box>
                    </Stack>
                  )
                })}
              </Stack>
            )}

            <Stack direction="row" spacing={1} sx={{ mt: 2 }}>
              <Button variant="outlined" size="small" disabled={busy} onClick={onQuality}>
                重新质检
              </Button>
              {onRefresh && (
                <Button variant="text" size="small" onClick={onRefresh}>
                  刷新数据
                </Button>
              )}
            </Stack>
          </CardContent>
        </Card>
      </Grid>
    </Grid>
  )
}
