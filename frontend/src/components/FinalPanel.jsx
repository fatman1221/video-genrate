import {
  Alert,
  Box,
  Button,
  Card,
  CardContent,
  Chip,
  Divider,
  Grid,
  Stack,
  Typography,
} from '@mui/material'
import DownloadIcon from '@mui/icons-material/Download'
import PlayArrowIcon from '@mui/icons-material/PlayArrow'
import CheckCircleIcon from '@mui/icons-material/CheckCircle'
import ErrorIcon from '@mui/icons-material/Error'
import WarningAmberIcon from '@mui/icons-material/WarningAmber'
import { fmtBytes, fmtDuration } from '../api'

const CHECK_ICON = {
  PASS: { Icon: CheckCircleIcon, color: 'success.main' },
  FAIL: { Icon: ErrorIcon, color: 'error.main' },
  WARN: { Icon: WarningAmberIcon, color: 'warning.main' },
}

export default function FinalPanel({ asset, quality, onCompose, onQuality, busy }) {
  return (
    <Grid container spacing={2.5}>
      <Grid item xs={12} lg={7}>
        <Card>
          <CardContent>
            <Typography variant="h6" sx={{ mb: 1.5 }}>
              Final Video
            </Typography>
            {asset?.url ? (
              <>
                <Box
                  component="video"
                  src={asset.url}
                  controls
                  poster={asset.extra?.poster_url}
                  sx={{ width: '100%', borderRadius: 2, bgcolor: '#000', aspectRatio: '16/9' }}
                />
                <Stack direction="row" spacing={2} sx={{ mt: 2 }} flexWrap="wrap">
                  {[
                    ['时长', fmtDuration(asset.duration)],
                    ['分辨率', `${asset.width}x${asset.height}`],
                    ['FPS', asset.fps],
                    ['大小', fmtBytes(asset.size_bytes)],
                    ['编码', asset.format?.toUpperCase()],
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
                <Stack direction="row" spacing={1.5} sx={{ mt: 2 }}>
                  <Button
                    variant="contained"
                    startIcon={<PlayArrowIcon />}
                    href={asset.url}
                    target="_blank"
                  >
                    全屏播放
                  </Button>
                  <Button
                    variant="outlined"
                    startIcon={<DownloadIcon />}
                    href={`/api/assets/${asset.asset_id}/download`}
                  >
                    下载成片
                  </Button>
                </Stack>
                <Typography variant="caption" color="text.secondary" sx={{ mt: 1.5, display: 'block' }}>
                  {asset.url}
                </Typography>
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
              <Typography variant="body2" color="text.secondary">
                尚未执行质量检查。
              </Typography>
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
            </Stack>
          </CardContent>
        </Card>
      </Grid>
    </Grid>
  )
}
