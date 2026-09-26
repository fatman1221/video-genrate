import {
  Box,
  Button,
  Card,
  CardContent,
  Chip,
  Divider,
  IconButton,
  Stack,
  Tooltip,
  Typography,
} from '@mui/material'
import ImageIcon from '@mui/icons-material/Image'
import VideocamIcon from '@mui/icons-material/Videocam'
import MicIcon from '@mui/icons-material/Mic'
import SubtitlesIcon from '@mui/icons-material/Subtitles'
import RefreshIcon from '@mui/icons-material/Refresh'
import DownloadIcon from '@mui/icons-material/Download'
import { statusColor } from '../api'

function AssetThumb({ asset, kind }) {
  if (!asset?.url) {
    return (
      <Box
        sx={{
          width: '100%',
          aspectRatio: '16/9',
          borderRadius: 2,
          bgcolor: 'rgba(124,77,255,0.06)',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          color: 'text.disabled',
          border: '1px dashed rgba(124,77,255,0.25)',
        }}
      >
        {kind === 'video' ? <VideocamIcon /> : kind === 'voice' ? <MicIcon /> : <ImageIcon />}
      </Box>
    )
  }
  if (kind === 'video') {
    return (
      <Box
        component="video"
        src={asset.url}
        controls
        preload="metadata"
        sx={{ width: '100%', aspectRatio: '16/9', borderRadius: 2, bgcolor: '#000' }}
      />
    )
  }
  if (kind === 'voice') {
    return (
      <Box sx={{ py: 1.5 }}>
        <Box component="audio" src={asset.url} controls sx={{ width: '100%', height: 34 }} />
      </Box>
    )
  }
  return (
    <Box
      component="img"
      src={asset.url}
      alt={asset.name}
      sx={{ width: '100%', aspectRatio: '16/9', objectFit: 'cover', borderRadius: 2, bgcolor: '#f2f0f8' }}
    />
  )
}

export default function ShotGrid({ shots = [], onRegenerate, busyId }) {
  if (!shots.length) {
    return (
      <Typography color="text.secondary" sx={{ py: 4, textAlign: 'center' }}>
        还没有镜头
      </Typography>
    )
  }

  return (
    <Box
      sx={{
        display: 'grid',
        gridTemplateColumns: { xs: '1fr', md: 'repeat(2, 1fr)', xl: 'repeat(3, 1fr)' },
        gap: 2,
      }}
    >
      {shots.map((shot) => {
        const assets = shot.assets || {}
        return (
          <Card key={shot.shot_id} sx={{ display: 'flex', flexDirection: 'column' }}>
            <CardContent sx={{ pb: 1.5 }}>
              <Stack direction="row" alignItems="center" justifyContent="space-between" sx={{ mb: 0.5 }}>
                <Stack direction="row" spacing={1} alignItems="center">
                  <Typography variant="subtitle1" fontWeight={700}>
                    {shot.code}
                  </Typography>
                  <Chip size="small" color={statusColor(shot.status)} label={shot.status} />
                </Stack>
                <Typography variant="caption" color="text.secondary">
                  {shot.duration}s · {shot.camera || '镜头'}
                </Typography>
              </Stack>

              <AssetThumb asset={assets.image} kind="image" />

              <Typography variant="body2" sx={{ mt: 1.2, minHeight: 40 }} color="text.primary">
                {shot.description}
              </Typography>

              <Divider sx={{ my: 1 }} />

              <Stack spacing={0.7}>
                <Typography variant="caption" color="text.secondary">
                  图像 Prompt
                </Typography>
                <Typography variant="caption" sx={{ display: 'block', opacity: 0.85 }}>
                  {shot.image_prompt?.slice(0, 120) || '—'}
                </Typography>
                {shot.voice_script && (
                  <>
                    <Typography variant="caption" color="text.secondary" sx={{ mt: 0.5 }}>
                      旁白
                    </Typography>
                    <Typography variant="caption" sx={{ display: 'block', opacity: 0.85 }}>
                      {shot.voice_script}
                    </Typography>
                  </>
                )}
              </Stack>
            </CardContent>

            <Box sx={{ px: 2, pb: 1.5, mt: 'auto' }}>
              <Stack direction="row" spacing={1} sx={{ mb: 1 }}>
                <Chip
                  size="small"
                  variant={assets.image ? 'filled' : 'outlined'}
                  color={assets.image ? 'success' : 'default'}
                  icon={<ImageIcon />}
                  label="图"
                />
                <Chip
                  size="small"
                  variant={assets.video ? 'filled' : 'outlined'}
                  color={assets.video ? 'success' : 'default'}
                  icon={<VideocamIcon />}
                  label="视频"
                />
                <Chip
                  size="small"
                  variant={assets.voice ? 'filled' : 'outlined'}
                  color={assets.voice ? 'success' : 'default'}
                  icon={<MicIcon />}
                  label="配音"
                />
                <Chip
                  size="small"
                  variant={assets.subtitle ? 'filled' : 'outlined'}
                  color={assets.subtitle ? 'success' : 'default'}
                  icon={<SubtitlesIcon />}
                  label="字幕"
                />
              </Stack>

              {assets.video?.url && <AssetThumb asset={assets.video} kind="video" />}
              {assets.voice?.url && <AssetThumb asset={assets.voice} kind="voice" />}

              {shot.last_error && (
                <Typography variant="caption" color="error" sx={{ display: 'block', mt: 1 }}>
                  {shot.last_error}
                </Typography>
              )}

              <Stack direction="row" spacing={1} sx={{ mt: 1.2 }}>
                <Button
                  size="small"
                  variant="outlined"
                  startIcon={<RefreshIcon />}
                  disabled={busyId === shot.shot_id}
                  onClick={() => onRegenerate(shot, 'image')}
                >
                  重生成图
                </Button>
                <Button
                  size="small"
                  variant="outlined"
                  startIcon={<RefreshIcon />}
                  disabled={busyId === shot.shot_id}
                  onClick={() => onRegenerate(shot, 'video')}
                >
                  重生成视频
                </Button>
                {assets.video?.url && (
                  <Tooltip title="下载视频">
                    <IconButton size="small" href={assets.video.url} download target="_blank">
                      <DownloadIcon fontSize="small" />
                    </IconButton>
                  </Tooltip>
                )}
              </Stack>
            </Box>
          </Card>
        )
      })}
    </Box>
  )
}
