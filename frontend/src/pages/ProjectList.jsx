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
  FormControl,
  Grid,
  InputLabel,
  LinearProgress,
  MenuItem,
  Paper,
  Select,
  Snackbar,
  Stack,
  TextField,
  Tooltip,
  Typography,
} from '@mui/material'
import AddIcon from '@mui/icons-material/Add'
import AutoAwesomeMotionIcon from '@mui/icons-material/AutoAwesomeMotion'
import PlayArrowIcon from '@mui/icons-material/PlayArrow'
import ApprovalOutlinedIcon from '@mui/icons-material/ApprovalOutlined'
import ErrorOutlineIcon from '@mui/icons-material/ErrorOutline'
import { useNavigate } from 'react-router-dom'
import { SKILL, fmtDuration, fmtTime, statusColor, useApi } from '../api'
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
  requirement: '制作一个 5 分钟的 AI Agent 教学视频，漫画教学风格。',
  style: '漫画教学风格',
  target_duration: 300,
  shot_duration: 5,
  width: 1280,
  height: 720,
  fps: 24,
}

function ProjectCard({ p, onOpen }) {
  return (
    <Card sx={{ height: '100%', overflow: 'hidden' }}>
      <CardActionArea onClick={onOpen} sx={{ height: '100%' }}>
        {/* 封面 */}
        <Box
          sx={{
            position: 'relative',
            aspectRatio: '16 / 10',
            background: p.cover_url
              ? '#EDEBF4'
              : 'linear-gradient(135deg, #6D4AFF 0%, #9B7BFF 60%, #C0AEFF 100%)',
          }}
        >
          {p.cover_url ? (
            <Box
              component="img"
              src={p.cover_url}
              alt={p.name}
              sx={{ width: '100%', height: '100%', objectFit: 'cover' }}
            />
          ) : (
            <Stack alignItems="center" justifyContent="center" sx={{ height: '100%' }}>
              <AutoAwesomeMotionIcon sx={{ fontSize: 44, color: 'rgba(255,255,255,0.75)' }} />
            </Stack>
          )}
          <Box
            sx={{
              position: 'absolute', inset: 0,
              background: 'linear-gradient(180deg, rgba(16,12,32,0.28) 0%, rgba(16,12,32,0) 42%, rgba(16,12,32,0.5) 100%)',
            }}
          />
          <Stack direction="row" spacing={0.6} sx={{ position: 'absolute', top: 10, left: 10 }}>
            <Chip
              size="small"
              color={statusColor(p.status)}
              label={STATUS_LABEL[p.status] || p.status}
            />
            {p.episode_no > 0 && (
              <Chip
                size="small"
                label={`第 ${p.episode_no} 集`}
                sx={{ bgcolor: 'rgba(255,255,255,0.92)', color: 'primary.dark', fontWeight: 700 }}
              />
            )}
          </Stack>
          {p.failed_tasks > 0 && (
            <Tooltip title={`${p.failed_tasks} 个失败任务`}>
              <Box
                sx={{
                  position: 'absolute', top: 10, right: 10, width: 24, height: 24, borderRadius: '50%',
                  bgcolor: 'error.main', color: '#fff', display: 'flex', alignItems: 'center',
                  justifyContent: 'center',
                }}
              >
                <ErrorOutlineIcon sx={{ fontSize: 15 }} />
              </Box>
            </Tooltip>
          )}
          {p.pending_review > 0 && p.failed_tasks === 0 && (p.progress || 0) >= 90 && (
            <Tooltip title={`${p.pending_review} 个节点待审核`}>
              <Box
                sx={{
                  position: 'absolute', top: 10, right: 10, width: 24, height: 24, borderRadius: '50%',
                  bgcolor: 'warning.main', color: '#fff', display: 'flex', alignItems: 'center',
                  justifyContent: 'center',
                }}
              >
                <ApprovalOutlinedIcon sx={{ fontSize: 15 }} />
              </Box>
            </Tooltip>
          )}
          <Box sx={{ position: 'absolute', bottom: 10, right: 10 }}>
            <MiniRing value={(p.progress || 0) / 100} />
          </Box>
          <Stack
            direction="row"
            spacing={0.8}
            alignItems="center"
            sx={{ position: 'absolute', bottom: 12, left: 12, color: 'rgba(255,255,255,0.92)' }}
          >
            <PlayArrowIcon sx={{ fontSize: 15 }} />
            <Typography sx={{ fontSize: 11.5, fontWeight: 600 }}>
              {p.current_node}
            </Typography>
          </Stack>
        </Box>

        {/* 信息 */}
        <Box sx={{ p: 2 }}>
          <Typography variant="h6" noWrap title={p.name}>
            {p.name}
          </Typography>
          <Typography
            variant="caption"
            color="text.secondary"
            sx={{
              display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical',
              overflow: 'hidden', minHeight: 32, mt: 0.4,
            }}
          >
            {p.requirement || p.description || '—'}
          </Typography>

          <Stack direction="row" alignItems="center" spacing={1} sx={{ mt: 1.4 }}>
            <LinearProgress
              variant="determinate"
              value={p.progress || 0}
              sx={{ flex: 1, height: 4 }}
            />
            <Typography variant="caption" color="text.secondary" sx={{ fontVariantNumeric: 'tabular-nums' }}>
              {p.nodes_done}/{p.nodes_total} 节点
            </Typography>
          </Stack>

          <Stack direction="row" spacing={0.8} alignItems="center" sx={{ mt: 1.2 }}>
            <Chip size="small" variant="outlined" label={`${p.shot_count} 镜头`} />
            <Chip size="small" variant="outlined" label={`${p.asset_count} 素材`} />
            <Box sx={{ flex: 1 }} />
            <Typography variant="caption" color="text.disabled">
              {fmtTime(p.updated_at)}
            </Typography>
          </Stack>
        </Box>
      </CardActionArea>
    </Card>
  )
}

export default function ProjectList() {
  const navigate = useNavigate()
  const { data, loading, reload } = useApi('/api/projects', { limit: 100 }, { poll: 5000 })
  const [open, setOpen] = useState(false)
  const [form, setForm] = useState(emptyForm)
  const [busy, setBusy] = useState(false)
  const [toast, setToast] = useState(null)

  const projects = data?.items || []

  const submit = async () => {
    if (!form.name.trim()) {
      setToast({ type: 'warning', text: '请填写项目名称' })
      return
    }
    setBusy(true)
    try {
      const res = await SKILL.bootstrap({
        ...form,
        target_duration: Number(form.target_duration),
        shot_duration: Number(form.shot_duration),
        generate_references: true,
      })
      if (!res.ok) throw new Error(res.error)
      setOpen(false)
      setForm(emptyForm)
      reload()
      navigate(`/projects/${res.data.project_id}`)
    } catch (err) {
      setToast({ type: 'error', text: `创建失败：${err.message}` })
    } finally {
      setBusy(false)
    }
  }

  const stats = [
    { label: '项目', value: projects.length },
    { label: '生成中', value: projects.filter((p) => p.status === 'GENERATING').length },
    { label: '已完成', value: projects.filter((p) => p.status === 'COMPLETED').length },
    { label: '镜头', value: projects.reduce((s, p) => s + (p.shot_count || 0), 0) },
    { label: '素材', value: projects.reduce((s, p) => s + (p.asset_count || 0), 0) },
  ]

  return (
    <Box>
      <Stack direction={{ xs: 'column', sm: 'row' }} spacing={2} alignItems={{ sm: 'flex-end' }} sx={{ mb: 3 }}>
        <Box sx={{ flex: 1 }}>
          <Typography variant="h3" sx={{ fontSize: 30 }}>
            AI Video Studio
          </Typography>
          <Typography variant="body2" color="text.secondary" sx={{ mt: 0.8 }}>
            Agent 负责思考与调度 · 工作台负责任务、素材、状态与执行
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
            新建项目
          </Button>
        </Stack>
      </Stack>

      {loading && <LinearProgress sx={{ mb: 2 }} />}

      {projects.length === 0 ? (
        <Paper sx={{ py: 8, textAlign: 'center', border: '1px dashed', borderColor: 'divider' }}>
          <AutoAwesomeMotionIcon sx={{ fontSize: 52, color: 'primary.light', mb: 1 }} />
          <Typography variant="h6">还没有项目</Typography>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 2.5, mt: 0.5 }}>
            输入一句需求，工作台会自动生成脚本、分镜与人物骨架
          </Typography>
          <Button variant="contained" startIcon={<AddIcon />} onClick={() => setOpen(true)}>
            新建项目
          </Button>
        </Paper>
      ) : (
        <Grid container spacing={2.5}>
          {projects.map((p) => (
            <Grid item xs={12} sm={6} lg={4} xl={3} key={p.id}>
              <ProjectCard p={p} onOpen={() => navigate(`/projects/${p.id}`)} />
            </Grid>
          ))}
        </Grid>
      )}

      <Dialog open={open} onClose={() => !busy && setOpen(false)} maxWidth="sm" fullWidth>
        <DialogTitle sx={{ fontSize: 17 }}>新建项目</DialogTitle>
        <DialogContent dividers>
          <Stack spacing={2} sx={{ pt: 1 }}>
            <TextField
              label="项目名称" value={form.name} fullWidth required
              onChange={(e) => setForm({ ...form, name: e.target.value })}
              placeholder="AI Agent 教学视频"
            />
            <TextField
              label="需求描述" value={form.requirement} multiline minRows={2} fullWidth
              onChange={(e) => setForm({ ...form, requirement: e.target.value })}
              helperText="Agent 会据此生成脚本与分镜；也可以之后再改写"
            />
            <Grid container spacing={2}>
              <Grid item xs={6}>
                <TextField
                  label="目标时长（秒）" type="number" fullWidth
                  value={form.target_duration}
                  onChange={(e) => setForm({ ...form, target_duration: e.target.value })}
                />
              </Grid>
              <Grid item xs={6}>
                <TextField
                  label="单镜头时长（秒）" type="number" fullWidth
                  value={form.shot_duration}
                  onChange={(e) => setForm({ ...form, shot_duration: e.target.value })}
                  helperText={`约 ${Math.max(4, Math.round(form.target_duration / Math.max(form.shot_duration, 1)))} 个镜头`}
                />
              </Grid>
              <Grid item xs={12}>
                <TextField
                  label="视觉风格" fullWidth value={form.style}
                  onChange={(e) => setForm({ ...form, style: e.target.value })}
                />
              </Grid>
              <Grid item xs={4}>
                <FormControl fullWidth>
                  <InputLabel>分辨率</InputLabel>
                  <Select
                    label="分辨率"
                    value={`${form.width}x${form.height}`}
                    onChange={(e) => {
                      const [w, h] = e.target.value.split('x')
                      setForm({ ...form, width: Number(w), height: Number(h) })
                    }}
                  >
                    <MenuItem value="1280x720">1280x720</MenuItem>
                    <MenuItem value="1920x1080">1920x1080</MenuItem>
                    <MenuItem value="960x540">960x540（快速）</MenuItem>
                  </Select>
                </FormControl>
              </Grid>
              <Grid item xs={4}>
                <TextField
                  label="帧率" type="number" fullWidth value={form.fps}
                  onChange={(e) => setForm({ ...form, fps: e.target.value })}
                />
              </Grid>
            </Grid>
          </Stack>
        </DialogContent>
        <DialogActions sx={{ px: 3, py: 2 }}>
          <Button onClick={() => setOpen(false)} disabled={busy}>取消</Button>
          <Button variant="contained" onClick={submit} disabled={busy}>
            {busy ? '创建中…' : '创建并生成骨架'}
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
