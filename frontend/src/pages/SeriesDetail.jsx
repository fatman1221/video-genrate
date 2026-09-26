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
  Divider,
  Grid,
  IconButton,
  LinearProgress,
  Paper,
  Snackbar,
  Stack,
  TextField,
  Tooltip,
  Typography,
} from '@mui/material'
import AddIcon from '@mui/icons-material/Add'
import ArrowBackIcon from '@mui/icons-material/ArrowBack'
import PlayArrowIcon from '@mui/icons-material/PlayArrow'
import PersonOutlineIcon from '@mui/icons-material/PersonOutline'
import AutoAwesomeMotionIcon from '@mui/icons-material/AutoAwesomeMotion'
import ErrorOutlineIcon from '@mui/icons-material/ErrorOutline'
import ApprovalOutlinedIcon from '@mui/icons-material/ApprovalOutlined'
import { useNavigate, useParams } from 'react-router-dom'
import { SKILL, fmtTime, useApi } from '../api'
import MiniRing from '../components/MiniRing'

const STATUS_LABEL = {
  DRAFT: '草稿',
  PLANNING: '规划中',
  GENERATING: '生成中',
  COMPLETED: '已完成',
  FAILED: '失败',
  PAUSED: '已暂停',
}

function EpisodeCard({ ep, onOpen }) {
  const done = ep.workflow_state === 'COMPLETED'
  return (
    <Card sx={{ height: '100%' }}>
      <CardActionArea onClick={onOpen} sx={{ height: '100%' }}>
        <Box sx={{ display: 'flex', height: '100%', minHeight: 128 }}>
          <Box
            sx={{
              width: 76,
              flexShrink: 0,
              display: 'flex',
              flexDirection: 'column',
              alignItems: 'center',
              justifyContent: 'center',
              gap: 0.6,
              bgcolor: done ? 'success.main' : 'primary.main',
              color: '#fff',
            }}
          >
            <Typography sx={{ fontSize: 22, fontWeight: 700, lineHeight: 1 }}>
              {ep.episode_no || '—'}
            </Typography>
            <Typography sx={{ fontSize: 10, opacity: 0.9 }}>集</Typography>
          </Box>

          <Box sx={{ flex: 1, p: 1.8, minWidth: 0 }}>
            <Stack direction="row" alignItems="center" spacing={0.8}>
              <Typography variant="subtitle2" noWrap sx={{ flex: 1, fontWeight: 600 }} title={ep.name}>
                {ep.name}
              </Typography>
              {ep.failed_tasks > 0 && <ErrorOutlineIcon sx={{ fontSize: 15, color: 'error.main' }} />}
              {ep.failed_tasks === 0 && ep.pending_review > 0 && (
                <ApprovalOutlinedIcon sx={{ fontSize: 15, color: 'warning.main' }} />
              )}
            </Stack>

            <Typography
              variant="caption"
              color="text.secondary"
              sx={{
                display: '-webkit-box',
                WebkitLineClamp: 2,
                WebkitBoxOrient: 'vertical',
                overflow: 'hidden',
                minHeight: 30,
                mt: 0.3,
              }}
            >
              {ep.requirement || ep.description || '—'}
            </Typography>

            <Stack direction="row" alignItems="center" spacing={1} sx={{ mt: 1 }}>
              <LinearProgress variant="determinate" value={ep.progress || 0} sx={{ flex: 1, height: 4 }} />
              <Typography variant="caption" color="text.secondary" sx={{ fontVariantNumeric: 'tabular-nums' }}>
                {ep.progress || 0}%
              </Typography>
            </Stack>

            <Stack direction="row" spacing={0.6} alignItems="center" sx={{ mt: 1 }}>
              <Chip
                size="small"
                color={done ? 'success' : 'default'}
                variant={done ? 'filled' : 'outlined'}
                label={STATUS_LABEL[ep.status] || ep.status}
                sx={{ height: 20, fontSize: 10.5 }}
              />
              <Typography variant="caption" color="text.disabled" noWrap>
                {ep.current_node} · {ep.shot_count} 镜头
              </Typography>
            </Stack>
          </Box>
        </Box>
      </CardActionArea>
    </Card>
  )
}

function CharacterCard({ c }) {
  const ref = c.reference_asset?.url
  return (
    <Card sx={{ overflow: 'hidden' }}>
      <Box
        sx={{
          aspectRatio: '1 / 1',
          bgcolor: 'rgba(109,74,255,0.06)',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
        }}
      >
        {ref ? (
          <Box component="img" src={ref} alt={c.name} sx={{ width: '100%', height: '100%', objectFit: 'cover' }} />
        ) : (
          <PersonOutlineIcon sx={{ fontSize: 40, color: 'primary.light' }} />
        )}
      </Box>
      <Box sx={{ p: 1.6 }}>
        <Stack direction="row" alignItems="center" spacing={0.6}>
          <Typography variant="subtitle2" noWrap sx={{ fontWeight: 600, flex: 1 }}>
            {c.name}
          </Typography>
          <Chip
            size="small"
            variant="outlined"
            color={c.role === 'protagonist' ? 'primary' : 'default'}
            label={c.role === 'protagonist' ? '主角' : '配角'}
            sx={{ height: 19, fontSize: 10 }}
          />
        </Stack>
        <Typography
          variant="caption"
          color="text.secondary"
          sx={{
            display: '-webkit-box',
            WebkitLineClamp: 2,
            WebkitBoxOrient: 'vertical',
            overflow: 'hidden',
            mt: 0.4,
            minHeight: 30,
          }}
        >
          {c.appearance || c.description || '—'}
        </Typography>
      </Box>
    </Card>
  )
}

export default function SeriesDetail() {
  const { seriesId } = useParams()
  const navigate = useNavigate()
  const { data, loading, reload } = useApi(`/api/series/${seriesId}`, null, { poll: 6000 })
  const [open, setOpen] = useState(false)
  const [req, setReq] = useState('')
  const [busy, setBusy] = useState(false)
  const [toast, setToast] = useState(null)

  const s = data?.series
  const episodes = s?.episodes || []
  const characters = s?.characters || []

  const addEpisode = async () => {
    setBusy(true)
    try {
      const res = await SKILL.createEpisode({ series_id: seriesId, requirement: req })
      if (!res.ok) throw new Error(res.error)
      setOpen(false)
      setReq('')
      reload()
      navigate(`/projects/${res.data.project_id}`)
    } catch (err) {
      setToast({ type: 'error', text: `新建失败：${err.message}` })
    } finally {
      setBusy(false)
    }
  }

  if (!s) {
    return (
      <Box>
        <Button startIcon={<ArrowBackIcon />} onClick={() => navigate('/series')} sx={{ mb: 2 }}>
          返回连续剧
        </Button>
        {loading ? <LinearProgress /> : <Alert severity="warning">未找到该连续剧</Alert>}
      </Box>
    )
  }

  const nextNo = (episodes.reduce((m, e) => Math.max(m, e.episode_no || 0), 0) || 0) + 1

  return (
    <Box>
      <Button startIcon={<ArrowBackIcon />} onClick={() => navigate('/series')} sx={{ mb: 2 }}>
        返回连续剧
      </Button>

      {/* 系列概览 */}
      <Paper sx={{ p: 3, mb: 3 }}>
        <Stack direction={{ xs: 'column', md: 'row' }} spacing={3} alignItems={{ md: 'center' }}>
          <Box sx={{ flex: 1, minWidth: 0 }}>
            <Stack direction="row" alignItems="center" spacing={1.2}>
              <Typography variant="h4" sx={{ fontSize: 24 }}>
                {s.name}
              </Typography>
              <Chip size="small" label={STATUS_LABEL[s.status] || s.status} />
            </Stack>
            <Typography variant="body2" color="text.secondary" sx={{ mt: 0.8 }}>
              {s.requirement || s.description || '—'}
            </Typography>
            <Stack direction="row" spacing={0.8} sx={{ mt: 1.6 }} flexWrap="wrap" useFlexGap>
              <Chip size="small" variant="outlined" label={`${s.episode_count} 集`} />
              <Chip
                size="small"
                variant="outlined"
                color={s.episodes_completed ? 'success' : 'default'}
                label={`${s.episodes_completed} 集完结`}
              />
              <Chip size="small" variant="outlined" label={`${characters.length} 个系列角色`} />
              <Chip size="small" variant="outlined" label={`每集 ${Math.round(s.episode_duration / 60)} 分钟`} />
              <Chip size="small" variant="outlined" label={`${s.width}×${s.height} @ ${s.fps}fps`} />
            </Stack>
          </Box>

          <Stack direction="row" spacing={3} alignItems="center">
            <Box sx={{ textAlign: 'center' }}>
              <MiniRing
                value={(s.progress || 0) / 100}
                size={64}
                stroke={6}
                color="#6D4AFF"
                trackColor="rgba(109,74,255,0.14)"
                textColor="#3C3489"
              />
              <Typography variant="caption" color="text.disabled" sx={{ display: 'block', mt: 0.6 }}>
                整体进度
              </Typography>
            </Box>
            <Button variant="contained" size="large" startIcon={<AddIcon />} onClick={() => setOpen(true)}>
              新建第 {nextNo} 集
            </Button>
          </Stack>
        </Stack>
      </Paper>

      {/* 分集 */}
      <Stack direction="row" alignItems="center" spacing={1.2} sx={{ mb: 1.6 }}>
        <Typography variant="h6" sx={{ fontSize: 16 }}>
          分集
        </Typography>
        <Typography variant="caption" color="text.disabled">
          每一集都是独立流水线，可单独回退 / 审核 / 重生成 / 成片
        </Typography>
      </Stack>

      <Grid container spacing={2} sx={{ mb: 4 }}>
        {episodes.map((ep) => (
          <Grid item xs={12} md={6} lg={4} key={ep.id}>
            <EpisodeCard ep={ep} onOpen={() => navigate(`/projects/${ep.id}`)} />
          </Grid>
        ))}
        <Grid item xs={12} md={6} lg={4}>
          <Card
            variant="outlined"
            sx={{
              height: '100%',
              minHeight: 128,
              borderStyle: 'dashed',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              cursor: 'pointer',
            }}
            onClick={() => setOpen(true)}
          >
            <Stack alignItems="center" spacing={0.6} sx={{ color: 'text.secondary', py: 3 }}>
              <AddIcon />
              <Typography variant="body2">新建第 {nextNo} 集</Typography>
            </Stack>
          </Card>
        </Grid>
      </Grid>

      <Divider sx={{ mb: 3 }} />

      {/* 系列级角色库 */}
      <Stack direction="row" alignItems="center" spacing={1.2} sx={{ mb: 1.6 }}>
        <Typography variant="h6" sx={{ fontSize: 16 }}>
          系列角色库
        </Typography>
        <Typography variant="caption" color="text.disabled">
          建在系列层，各集自动复用 —— 主角不会换集换脸
        </Typography>
      </Stack>

      {characters.length === 0 ? (
        <Paper sx={{ py: 5, textAlign: 'center', border: '1px dashed rgba(26,23,38,0.12)' }}>
          <PersonOutlineIcon sx={{ fontSize: 38, color: 'primary.light', mb: 0.6 }} />
          <Typography variant="body2" color="text.secondary">
            还没有系列级角色
          </Typography>
          <Typography variant="caption" color="text.disabled">
            可在任意一集里创建角色后「提升为系列级」，或直接让 Agent 建在系列下
          </Typography>
        </Paper>
      ) : (
        <Grid container spacing={2}>
          {characters.map((c) => (
            <Grid item xs={6} sm={4} md={3} lg={2} key={c.id}>
              <CharacterCard c={c} />
            </Grid>
          ))}
        </Grid>
      )}

      <Dialog open={open} onClose={() => !busy && setOpen(false)} maxWidth="sm" fullWidth>
        <DialogTitle sx={{ fontSize: 17 }}>新建第 {nextNo} 集</DialogTitle>
        <DialogContent dividers>
          <Stack spacing={2} sx={{ pt: 1 }}>
            <Alert severity="info" icon={false} sx={{ py: 0.4 }}>
              将自动继承本系列的风格、画幅、每集时长；集号自动递增。
            </Alert>
            <TextField
              label="本集要讲什么（可选）"
              value={req}
              multiline
              minRows={3}
              fullWidth
              onChange={(e) => setReq(e.target.value)}
              placeholder="第 1 集：用「实习生 vs 会自己干活的助手」讲清楚 Agent 与传统脚本的区别"
              helperText="留空则沿用整部剧的总需求"
            />
          </Stack>
        </DialogContent>
        <DialogActions sx={{ px: 3, py: 2 }}>
          <Button onClick={() => setOpen(false)} disabled={busy}>取消</Button>
          <Button variant="contained" onClick={addEpisode} disabled={busy}>
            {busy ? '创建中…' : '创建并打开'}
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
