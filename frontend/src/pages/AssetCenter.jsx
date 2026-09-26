import { useMemo, useState } from 'react'
import {
  Alert,
  Box,
  Button,
  Chip,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Divider,
  FormControl,
  Grid,
  InputLabel,
  MenuItem,
  Select,
  Slider,
  Stack,
  TextField,
  ToggleButton,
  ToggleButtonGroup,
  Typography,
} from '@mui/material'
import FaceOutlinedIcon from '@mui/icons-material/FaceOutlined'
import LandscapeOutlinedIcon from '@mui/icons-material/LandscapeOutlined'
import AudiotrackOutlinedIcon from '@mui/icons-material/AudiotrackOutlined'
import CollectionsOutlinedIcon from '@mui/icons-material/CollectionsOutlined'
import GraphicEqIcon from '@mui/icons-material/GraphicEq'
import AssetGrid from '../components/AssetGrid'
import { deleteAsset, fmtBytes, generateVoiceAsset, useApi } from '../api'

const GROUPS = [
  { value: 'character', label: '人物图', icon: FaceOutlinedIcon, hint: '角色参考图与人物设定图' },
  { value: 'scene', label: '场景图', icon: LandscapeOutlinedIcon, hint: '场景概念图与背景图' },
  { value: 'audio', label: '音频', icon: AudiotrackOutlinedIcon, hint: '配音 / 配乐 / 音效，可直接合成语音' },
  { value: 'all', label: '全部', icon: CollectionsOutlinedIcon, hint: '素材中心的全部内容' },
]

/** 本机 macOS `say` 常用的中文音色，点击即填入。 */
const QUICK_VOICES = ['Tingting', 'Meijia', 'Sinji', 'Samantha', 'Daniel']

export default function AssetCenter() {
  const [group, setGroup] = useState('character')
  const [projectId, setProjectId] = useState('all')
  const [keyword, setKeyword] = useState('')
  const [onlyStandalone, setOnlyStandalone] = useState(false)
  const [voiceOpen, setVoiceOpen] = useState(false)
  const [toast, setToast] = useState(null)

  const params = useMemo(() => {
    const p = { limit: 300 }
    if (group !== 'all') p.group = group
    if (projectId !== 'all') p.project_id = projectId
    if (keyword.trim()) p.keyword = keyword.trim()
    if (onlyStandalone) p.unassigned = true
    return p
  }, [group, projectId, keyword, onlyStandalone])

  const { data, loading, error, reload } = useApi('/api/assets', params)
  const projects = useApi('/api/projects', { limit: 100 })

  const stats = data?.stats
  const assets = data?.items || []
  const active = GROUPS.find((g) => g.value === group)

  const removeAsset = async (a) => {
    if (!window.confirm(`确认删除素材「${a.name}」？文件会一并删除，不可恢复。`)) return
    try {
      await deleteAsset(a.asset_id)
      setToast({ type: 'success', text: `已删除「${a.name}」` })
      reload()
    } catch (err) {
      setToast({ type: 'error', text: `删除失败：${err.message}` })
    }
  }

  return (
    <Box>
      <Stack direction="row" alignItems="flex-end" spacing={2} sx={{ mb: 0.5 }}>
        <Typography variant="h5" fontWeight={700}>
          素材中心
        </Typography>
        <Typography variant="body2" color="text.secondary" sx={{ pb: 0.4 }}>
          跨项目聚合的人物图 / 场景图 / 音频，可直接合成语音并保存
        </Typography>
        <Box sx={{ flex: 1 }} />
        <Typography variant="caption" color="text.disabled">
          {stats ? `共 ${stats.total} 个素材 · ${fmtBytes(stats.total_size_bytes)}` : ''}
        </Typography>
      </Stack>

      <Divider sx={{ my: 2 }} />

      {toast && (
        <Alert severity={toast.type} sx={{ mb: 2 }} onClose={() => setToast(null)}>
          {toast.text}
        </Alert>
      )}
      {error && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}

      <Stack direction={{ xs: 'column', md: 'row' }} spacing={1.5} sx={{ mb: 2 }} alignItems={{ md: 'center' }}>
        <ToggleButtonGroup
          size="small"
          exclusive
          value={group}
          onChange={(e, v) => v && setGroup(v)}
          sx={{ flexWrap: 'wrap', gap: 0.8 }}
        >
          {GROUPS.map((g) => {
            const Icon = g.icon
            const count = g.value === 'all' ? stats?.total : stats?.groups?.[g.value]?.count
            return (
              <ToggleButton key={g.value} value={g.value} sx={{ gap: 0.6, textTransform: 'none' }}>
                <Icon sx={{ fontSize: 17 }} />
                {g.label}
                <Typography component="span" variant="caption" sx={{ color: 'text.disabled' }}>
                  {count ?? 0}
                </Typography>
              </ToggleButton>
            )
          })}
        </ToggleButtonGroup>

        <Box sx={{ flex: 1 }} />

        <FormControl size="small" sx={{ minWidth: 190 }}>
          <InputLabel>归属项目</InputLabel>
          <Select
            label="归属项目"
            value={projectId}
            onChange={(e) => setProjectId(e.target.value)}
          >
            <MenuItem value="all">全部项目</MenuItem>
            {(projects.data?.items || projects.data?.projects || []).map((p) => (
              <MenuItem key={p.project_id || p.id} value={p.project_id || p.id}>
                {p.title || p.name || p.project_id || p.id}
              </MenuItem>
            ))}
          </Select>
        </FormControl>

        <TextField
          size="small"
          label="搜索"
          value={keyword}
          onChange={(e) => setKeyword(e.target.value)}
          sx={{ width: 180 }}
        />

        <Button
          size="small"
          variant={onlyStandalone ? 'contained' : 'outlined'}
          onClick={() => setOnlyStandalone((v) => !v)}
        >
          仅独立素材
        </Button>

        {group === 'audio' && (
          <Button
            size="small"
            variant="contained"
            startIcon={<GraphicEqIcon />}
            onClick={() => setVoiceOpen(true)}
          >
            生成语音
          </Button>
        )}
      </Stack>

      {active && (
        <Typography variant="caption" color="text.disabled" sx={{ display: 'block', mb: 1.5 }}>
          {active.hint}
        </Typography>
      )}

      {loading && !data ? (
        <Typography color="text.secondary" sx={{ py: 4, textAlign: 'center' }}>
          加载中…
        </Typography>
      ) : (
        <AssetGrid assets={assets} busy={false} onDelete={removeAsset} showProject />
      )}

      <VoiceDialog
        open={voiceOpen}
        onClose={() => setVoiceOpen(false)}
        projects={projects.data?.items || projects.data?.projects || []}
        onDone={(msg) => {
          setToast({ type: 'success', text: msg })
          setVoiceOpen(false)
          setGroup('audio')
          reload()
        }}
      />
    </Box>
  )
}

/** 独立语音合成：文本 + 音色 + 语速 → 保存进素材库（可不归属任何项目）。 */
function VoiceDialog({ open, onClose, onDone, projects = [] }) {
  const [text, setText] = useState('')
  const [voice, setVoice] = useState('')
  const [rate, setRate] = useState(180)
  const [name, setName] = useState('')
  const [projectId, setProjectId] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState(null)

  const submit = async () => {
    if (!text.trim()) {
      setErr('请先填写要合成的文本')
      return
    }
    setBusy(true)
    setErr(null)
    try {
      const body = { text, voice: voice.trim(), rate, name: name.trim() }
      if (projectId) body.project_id = projectId
      const res = await generateVoiceAsset(body)
      const d = res?.data || {}
      onDone(`语音已生成并保存：${d.name || '未命名'}（${Number(d.duration || 0).toFixed(1)} 秒）`)
      setText('')
      setName('')
    } catch (e) {
      setErr(e?.response?.data?.detail || e.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Dialog open={open} onClose={busy ? undefined : onClose} maxWidth="sm" fullWidth>
      <DialogTitle>生成语音</DialogTitle>
      <DialogContent dividers>
        <Stack spacing={2} sx={{ pt: 0.5 }}>
          <Typography variant="caption" color="text.secondary">
            合成结果会直接保存到素材中心的「音频」分区。不选归属项目即为独立素材，不依赖任何项目。
          </Typography>

          <TextField
            label="文本"
            multiline
            minRows={3}
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder="要合成的旁白 / 台词……"
            fullWidth
          />

          <Grid container spacing={1.5}>
            <Grid item xs={12} sm={7}>
              <TextField
                size="small"
                label="音色"
                value={voice}
                onChange={(e) => setVoice(e.target.value)}
                placeholder="留空使用默认音色"
                fullWidth
              />
              <Stack direction="row" spacing={0.6} sx={{ mt: 1, flexWrap: 'wrap', gap: 0.6 }}>
                {QUICK_VOICES.map((v) => (
                  <Chip
                    key={v}
                    size="small"
                    label={v}
                    variant={voice === v ? 'filled' : 'outlined'}
                    color={voice === v ? 'primary' : 'default'}
                    onClick={() => setVoice(v)}
                  />
                ))}
              </Stack>
            </Grid>
            <Grid item xs={12} sm={5}>
              <Typography variant="caption" color="text.secondary">
                语速 {rate}
              </Typography>
              <Slider
                size="small"
                min={100}
                max={300}
                step={10}
                value={rate}
                onChange={(e, v) => setRate(v)}
              />
            </Grid>
          </Grid>

          <Stack direction="row" spacing={1.5}>
            <TextField
              size="small"
              label="素材名（可选）"
              value={name}
              onChange={(e) => setName(e.target.value)}
              fullWidth
            />
            <FormControl size="small" sx={{ minWidth: 200 }}>
              <InputLabel>归属项目（可选）</InputLabel>
              <Select
                label="归属项目（可选）"
                value={projectId}
                onChange={(e) => setProjectId(e.target.value)}
              >
                <MenuItem value="">不归属（独立素材）</MenuItem>
                {projects.map((p) => (
                  <MenuItem key={p.project_id || p.id} value={p.project_id || p.id}>
                    {p.title || p.name || p.project_id || p.id}
                  </MenuItem>
                ))}
              </Select>
            </FormControl>
          </Stack>

          {err && <Alert severity="error">{err}</Alert>}
        </Stack>
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose} disabled={busy}>
          取消
        </Button>
        <Button variant="contained" onClick={submit} disabled={busy}>
          {busy ? '合成中…' : '生成并保存'}
        </Button>
      </DialogActions>
    </Dialog>
  )
}
