import { useState } from 'react'
import {
  Alert,
  Box,
  Button,
  Card,
  CardActionArea,
  Chip,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Grid,
  LinearProgress,
  Paper,
  Snackbar,
  Stack,
  TextField,
  Typography,
} from '@mui/material'
import AddIcon from '@mui/icons-material/Add'
import SubscriptionsOutlinedIcon from '@mui/icons-material/SubscriptionsOutlined'
import MovieFilterOutlinedIcon from '@mui/icons-material/MovieFilterOutlined'
import PersonOutlineIcon from '@mui/icons-material/PersonOutline'
import { useNavigate } from 'react-router-dom'
import { SKILL, fmtTime, getSeriesList, statusColor, useApi } from '../api'
import MiniRing from '../components/MiniRing'

const STATUS_LABEL = {
  DRAFT: '草稿',
  PLANNING: '规划中',
  GENERATING: '生成中',
  COMPLETED: '已完成',
  FAILED: '失败',
  PAUSED: '已暂停',
}

const emptyForm = {
  name: '',
  requirement: '一部 12 集的 AI Agent 教学连续剧，漫画教学风格，每集 5 分钟。',
  style: '漫画教学风格',
  episode_duration: 300,
  planned_episodes: 12,
  episodes: 3,
}

function SeriesCard({ s, onOpen }) {
  return (
    <Card sx={{ height: '100%', overflow: 'hidden' }}>
      <CardActionArea onClick={onOpen} sx={{ height: '100%' }}>
        <Box
          sx={{
            position: 'relative',
            aspectRatio: '16 / 10',
            background: s.cover_url
              ? '#EDEBF4'
              : 'linear-gradient(135deg, #4B3BA8 0%, #6D4AFF 55%, #9B7BFF 100%)',
          }}
        >
          {s.cover_url ? (
            <Box
              component="img"
              src={s.cover_url}
              alt={s.name}
              sx={{ width: '100%', height: '100%', objectFit: 'cover' }}
            />
          ) : (
            <Stack alignItems="center" justifyContent="center" sx={{ height: '100%' }}>
              <SubscriptionsOutlinedIcon sx={{ fontSize: 46, color: 'rgba(255,255,255,0.78)' }} />
            </Stack>
          )}
          <Box
            sx={{
              position: 'absolute',
              inset: 0,
              background:
                'linear-gradient(180deg, rgba(16,12,32,0.3) 0%, rgba(16,12,32,0) 42%, rgba(16,12,32,0.55) 100%)',
            }}
          />
          <Chip
            size="small"
            color={statusColor(s.status)}
            label={STATUS_LABEL[s.status] || s.status}
            sx={{ position: 'absolute', top: 10, left: 10 }}
          />
          <Box sx={{ position: 'absolute', bottom: 10, right: 10 }}>
            <MiniRing value={(s.progress || 0) / 100} />
          </Box>
          <Stack
            direction="row"
            spacing={0.6}
            alignItems="center"
            sx={{ position: 'absolute', bottom: 12, left: 12, color: 'rgba(255,255,255,0.94)' }}
          >
            <MovieFilterOutlinedIcon sx={{ fontSize: 15 }} />
            <Typography sx={{ fontSize: 11.5, fontWeight: 600 }}>
              共 {s.episode_count} 集
              {s.planned_episodes ? ` / 计划 ${s.planned_episodes}` : ''}
            </Typography>
          </Stack>
        </Box>

        <Box sx={{ p: 2 }}>
          <Typography variant="h6" noWrap title={s.name}>
            {s.name}
          </Typography>
          <Typography
            variant="caption"
            color="text.secondary"
            sx={{
              display: '-webkit-box',
              WebkitLineClamp: 2,
              WebkitBoxOrient: 'vertical',
              overflow: 'hidden',
              minHeight: 32,
              mt: 0.4,
            }}
          >
            {s.requirement || s.description || '—'}
          </Typography>

          <Stack direction="row" alignItems="center" spacing={1} sx={{ mt: 1.4 }}>
            <LinearProgress variant="determinate" value={s.progress || 0} sx={{ flex: 1, height: 4 }} />
            <Typography variant="caption" color="text.secondary" sx={{ fontVariantNumeric: 'tabular-nums' }}>
              {s.episodes_completed}/{s.episode_count} 集完结
            </Typography>
          </Stack>

          <Stack direction="row" spacing={0.8} alignItems="center" sx={{ mt: 1.2 }}>
            <Chip
              size="small"
              variant="outlined"
              icon={<PersonOutlineIcon sx={{ fontSize: 14 }} />}
              label={`${s.character_count} 角色`}
            />
            <Chip size="small" variant="outlined" label={`每集 ${Math.round(s.episode_duration / 60)} 分钟`} />
            <Box sx={{ flex: 1 }} />
            <Typography variant="caption" color="text.disabled">
              {fmtTime(s.updated_at)}
            </Typography>
          </Stack>
        </Box>
      </CardActionArea>
    </Card>
  )
}

export default function SeriesList() {
  const navigate = useNavigate()
  const { data, loading, reload } = useApi('/api/series', { limit: 100 }, { poll: 6000 })
  const [open, setOpen] = useState(false)
  const [form, setForm] = useState(emptyForm)
  const [busy, setBusy] = useState(false)
  const [toast, setToast] = useState(null)

  const series = data?.items || []

  const submit = async () => {
    if (!form.name.trim()) {
      setToast({ type: 'warning', text: '请填写剧名' })
      return
    }
    setBusy(true)
    try {
      const res = await SKILL.createSeries({
        ...form,
        episode_duration: Number(form.episode_duration),
        planned_episodes: Number(form.planned_episodes),
        episodes: Number(form.episodes),
      })
      if (!res.ok) throw new Error(res.error)
      setOpen(false)
      setForm(emptyForm)
      reload()
      navigate(`/series/${res.data.series_id}`)
    } catch (err) {
      setToast({ type: 'error', text: `创建失败：${err.message}` })
    } finally {
      setBusy(false)
    }
  }

  const stats = [
    { label: '连续剧', value: series.length },
    { label: '总集数', value: series.reduce((n, s) => n + (s.episode_count || 0), 0) },
    { label: '已完结集', value: series.reduce((n, s) => n + (s.episodes_completed || 0), 0) },
    { label: '系列角色', value: series.reduce((n, s) => n + (s.character_count || 0), 0) },
  ]

  return (
    <Box>
      <Stack direction={{ xs: 'column', sm: 'row' }} spacing={2} alignItems={{ sm: 'flex-end' }} sx={{ mb: 3 }}>
        <Box sx={{ flex: 1 }}>
          <Typography variant="h3" sx={{ fontSize: 30 }}>
            连续剧
          </Typography>
          <Typography variant="body2" color="text.secondary" sx={{ mt: 0.8 }}>
            一部剧 → 多集 → 每集一条完整流水线 · 角色形象跨集复用
          </Typography>
        </Box>
        <Stack direction="row" spacing={2.5} alignItems="center">
          {stats.map((s) => (
            <Box key={s.label} sx={{ textAlign: 'right' }}>
              <Typography variant="h6" sx={{ fontSize: 19, lineHeight: 1.2 }}>
                {s.value}
              </Typography>
              <Typography variant="caption" color="text.disabled" sx={{ fontSize: 10.5 }}>
                {s.label}
              </Typography>
            </Box>
          ))}
          <Button variant="contained" size="large" startIcon={<AddIcon />} onClick={() => setOpen(true)}>
            新建连续剧
          </Button>
        </Stack>
      </Stack>

      {loading && <LinearProgress sx={{ mb: 2 }} />}

      {series.length === 0 ? (
        <Paper sx={{ py: 8, textAlign: 'center', border: '1px dashed rgba(26,23,38,0.12)' }}>
          <SubscriptionsOutlinedIcon sx={{ fontSize: 52, color: 'primary.light', mb: 1 }} />
          <Typography variant="h6">还没有连续剧</Typography>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 2.5, mt: 0.5 }}>
            建一部剧、定好角色，之后一集一集往下做
          </Typography>
          <Button variant="contained" startIcon={<AddIcon />} onClick={() => setOpen(true)}>
            新建连续剧
          </Button>
        </Paper>
      ) : (
        <Grid container spacing={2.5}>
          {series.map((s) => (
            <Grid item xs={12} sm={6} lg={4} xl={3} key={s.series_id}>
              <SeriesCard s={s} onOpen={() => navigate(`/series/${s.series_id}`)} />
            </Grid>
          ))}
        </Grid>
      )}

      <Dialog open={open} onClose={() => !busy && setOpen(false)} maxWidth="sm" fullWidth>
        <DialogTitle sx={{ fontSize: 17 }}>新建连续剧</DialogTitle>
        <DialogContent dividers>
          <Stack spacing={2} sx={{ pt: 1 }}>
            <TextField
              label="剧名" value={form.name} fullWidth required
              onChange={(e) => setForm({ ...form, name: e.target.value })}
              placeholder="AI Agent 从入门到实战"
            />
            <TextField
              label="整部剧的定位 / 总需求" value={form.requirement} multiline minRows={2} fullWidth
              onChange={(e) => setForm({ ...form, requirement: e.target.value })}
              helperText="每一集会继承这段描述，可再单独覆写"
            />
            <Grid container spacing={2}>
              <Grid item xs={6}>
                <TextField
                  label="每集时长（秒）" type="number" fullWidth value={form.episode_duration}
                  onChange={(e) => setForm({ ...form, episode_duration: e.target.value })}
                />
              </Grid>
              <Grid item xs={6}>
                <TextField
                  label="计划集数" type="number" fullWidth value={form.planned_episodes}
                  onChange={(e) => setForm({ ...form, planned_episodes: e.target.value })}
                  helperText="0 表示边做边加"
                />
              </Grid>
              <Grid item xs={6}>
                <TextField
                  label="先开几集" type="number" fullWidth value={form.episodes}
                  onChange={(e) => setForm({ ...form, episodes: e.target.value })}
                  helperText="只建空壳，不启动生成"
                />
              </Grid>
              <Grid item xs={6}>
                <TextField
                  label="视觉风格" fullWidth value={form.style}
                  onChange={(e) => setForm({ ...form, style: e.target.value })}
                />
              </Grid>
            </Grid>
          </Stack>
        </DialogContent>
        <DialogActions sx={{ px: 3, py: 2 }}>
          <Button onClick={() => setOpen(false)} disabled={busy}>取消</Button>
          <Button variant="contained" onClick={submit} disabled={busy}>
            {busy ? '创建中…' : '创建连续剧'}
          </Button>
        </DialogActions>
      </Dialog>

      <Snackbar
        open={Boolean(toast)}
        autoHideDuration={5000}
        onClose={() => setToast(null)}
        anchorOrigin={{ vertical: 'bottom', horizontal: 'center' }}
      >
        {toast ? <Alert severity={toast.type}>{toast.text}</Alert> : undefined}
      </Snackbar>
    </Box>
  )
}
