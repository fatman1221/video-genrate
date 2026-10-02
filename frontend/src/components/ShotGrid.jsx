import { useState } from 'react'
import {
  Box,
  Button,
  Card,
  CardContent,
  Chip,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Divider,
  FormControl,
  Grid,
  IconButton,
  InputLabel,
  MenuItem,
  Select,
  Stack,
  TextField,
  Tooltip,
  Typography,
} from '@mui/material'
import ImageIcon from '@mui/icons-material/Image'
import VideocamIcon from '@mui/icons-material/Videocam'
import MicIcon from '@mui/icons-material/Mic'
import SubtitlesIcon from '@mui/icons-material/Subtitles'
import RefreshIcon from '@mui/icons-material/Refresh'
import EditOutlinedIcon from '@mui/icons-material/EditOutlined'
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
          bgcolor: 'action.hover',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          color: 'text.disabled',
          border: '1px dashed',
          borderColor: 'primary.main',
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
      sx={{ width: '100%', aspectRatio: '16/9', objectFit: 'cover', borderRadius: 2, bgcolor: 'action.hover' }}
    />
  )
}

/** 单镜头编辑弹窗：提示词 / 台词 / 音色 / 情感指令 / 时长，改完可就地重生成。 */
function ShotEditor({ shot, open, onClose, speakers, busy, onRegenerate, onSave }) {
  const [form, setForm] = useState(null)

  // 打开时从镜头初始化表单；关闭则丢弃草稿
  const init = () => ({
    image_prompt: shot?.image_prompt || '',
    video_prompt: shot?.video_prompt || '',
    voice_script: shot?.voice_script || '',
    voice_speaker: shot?.voice_speaker || '',
    voice_instruct: shot?.voice_instruct || '',
    duration: shot?.duration ?? '',
  })
  const f = form || init()
  const set = (k, v) => setForm({ ...f, [k]: v })

  // 音色选项：引擎清单 + 镜头当前值（清单加载慢或音色已下线时也不会显示成空）
  const opts = [...(speakers || [])]
  if (f.voice_speaker && !opts.some((s) => s.name === f.voice_speaker)) {
    opts.push({ name: f.voice_speaker, label: `${f.voice_speaker}（当前使用）` })
  }

  if (!shot) return null

  return (
    <Dialog open={open} onClose={onClose} maxWidth="md" fullWidth>
      <DialogTitle sx={{ pb: 1 }}>
        <Stack direction="row" spacing={1} alignItems="center">
          <Chip size="small" color="primary" label={shot.code} />
          <Typography variant="h6" sx={{ fontSize: 17 }}>镜头编辑</Typography>
          <Chip size="small" color={statusColor(shot.status)} label={shot.status} variant="outlined" />
        </Stack>
      </DialogTitle>
      <DialogContent dividers>
        <Stack spacing={2} sx={{ pt: 0.5 }}>
          <TextField
            label="镜头描述"
            size="small"
            fullWidth
            value={shot.description || ''}
            InputProps={{ readOnly: true }}
            helperText="画面内容由分镜决定；如需调整请改图像/视频提示词"
          />
          <Grid container spacing={2}>
            <Grid item xs={12} sm={6}>
              <TextField
                label="时长（秒）"
                size="small"
                type="number"
                fullWidth
                value={f.duration}
                onChange={(e) => set('duration', e.target.value === '' ? '' : Number(e.target.value))}
                inputProps={{ step: 0.5, min: 1 }}
              />
            </Grid>
            <Grid item xs={12} sm={6}>
              <FormControl fullWidth size="small">
                <InputLabel>配音音色</InputLabel>
                <Select
                  label="配音音色"
                  value={f.voice_speaker}
                  onChange={(e) => set('voice_speaker', e.target.value)}
                >
                  <MenuItem value=""><em>用项目默认</em></MenuItem>
                  {opts.map((s) => (
                    <MenuItem key={s.name} value={s.name}>{s.label || s.name}</MenuItem>
                  ))}
                </Select>
              </FormControl>
            </Grid>
          </Grid>

          <TextField
            label="图像 Prompt"
            size="small"
            fullWidth
            multiline
            minRows={3}
            value={f.image_prompt}
            onChange={(e) => set('image_prompt', e.target.value)}
          />
          <TextField
            label="视频 Prompt（图生视频的运动描述）"
            size="small"
            fullWidth
            multiline
            minRows={2}
            value={f.video_prompt}
            onChange={(e) => set('video_prompt', e.target.value)}
          />
          <Divider textAlign="left">
            <Typography variant="caption" color="text.disabled">配音</Typography>
          </Divider>
          <TextField
            label="旁白台词"
            size="small"
            fullWidth
            multiline
            minRows={2}
            value={f.voice_script}
            onChange={(e) => set('voice_script', e.target.value)}
          />
          <TextField
            label="情感指令"
            size="small"
            fullWidth
            value={f.voice_instruct}
            onChange={(e) => set('voice_instruct', e.target.value)}
            placeholder="例：用东北口音唠嗑，活泼自然，声音明亮清晰"
          />

          <Stack direction="row" spacing={1} flexWrap="wrap" sx={{ gap: 1 }}>
            <Button
              size="small"
              variant="outlined"
              startIcon={<RefreshIcon />}
              disabled={busy}
              onClick={() => onRegenerate(shot, 'image')}
            >
              重生成图
            </Button>
            <Button
              size="small"
              variant="outlined"
              startIcon={<RefreshIcon />}
              disabled={busy}
              onClick={() => onRegenerate(shot, 'video')}
            >
              重生成视频
            </Button>
            <Button
              size="small"
              variant="outlined"
              startIcon={<MicIcon />}
              disabled={busy}
              onClick={() => onRegenerate(shot, 'voice', {
                voice_script: f.voice_script,
                voice_speaker: f.voice_speaker,
                voice_instruct: f.voice_instruct,
              })}
            >
              重生成配音
            </Button>
          </Stack>
        </Stack>
      </DialogContent>
      <DialogActions sx={{ px: 3, py: 1.5 }}>
        <Button onClick={() => { setForm(null); onClose() }}>取消</Button>
        <Button
          variant="contained"
          disabled={busy}
          onClick={async () => {
            const payload = { shot_id: shot.shot_id, ...f }
            if (payload.duration === '') delete payload.duration
            await onSave(payload)
            setForm(null)
            onClose()
          }}
        >
          保存
        </Button>
      </DialogActions>
    </Dialog>
  )
}

export default function ShotGrid({
  shots = [], onRegenerate, busyId, speakers = [], onSaveShot, busy,
}) {
  const [editing, setEditing] = useState(null)

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
        const voiceOver = assets.voice?.duration && shot.duration
          && assets.voice.duration > shot.duration + 0.4
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
                <Stack direction="row" spacing={0.4} alignItems="center">
                  <Typography variant="caption" color="text.secondary">
                    {shot.duration}s · {shot.camera || '镜头'}
                  </Typography>
                  <Tooltip title="编辑提示词 / 台词 / 音色">
                    <IconButton size="small" onClick={() => setEditing(shot)}>
                      <EditOutlinedIcon fontSize="small" />
                    </IconButton>
                  </Tooltip>
                </Stack>
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
                    <Stack direction="row" spacing={0.6} alignItems="center" sx={{ mt: 0.5 }}>
                      <Typography variant="caption" color="text.secondary">旁白</Typography>
                      {shot.voice_speaker && (
                        <Chip size="small" variant="outlined" label={shot.voice_speaker} sx={{ height: 18 }} />
                      )}
                      {voiceOver && <Chip size="small" color="warning" label="超长" sx={{ height: 18 }} />}
                    </Stack>
                    <Typography variant="caption" sx={{ display: 'block', opacity: 0.85 }}>
                      {shot.voice_script}
                    </Typography>
                  </>
                )}
                {shot.voice_instruct && (
                  <Typography variant="caption" color="text.disabled" sx={{ display: 'block', mt: 0.3 }}>
                    指令：{shot.voice_instruct.slice(0, 46)}
                    {shot.voice_instruct.length > 46 ? '…' : ''}
                  </Typography>
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

              <Stack direction="row" spacing={1} sx={{ mt: 1.2, flexWrap: 'wrap', gap: 1 }}>
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
                <Button
                  size="small"
                  variant="outlined"
                  startIcon={<MicIcon />}
                  disabled={busyId === shot.shot_id}
                  onClick={() => onRegenerate(shot, 'voice')}
                >
                  重生成配音
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

      <ShotEditor
        shot={editing}
        open={Boolean(editing)}
        onClose={() => setEditing(null)}
        speakers={speakers}
        busy={busy}
        onRegenerate={onRegenerate}
        onSave={onSaveShot}
      />
    </Box>
  )
}
