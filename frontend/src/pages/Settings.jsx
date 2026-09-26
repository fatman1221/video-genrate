import { useEffect, useState } from 'react'
import {
  Alert,
  Box,
  Button,
  Chip,
  Divider,
  FormControlLabel,
  Grid,
  Paper,
  Stack,
  Switch,
  TextField,
  Tooltip,
  Typography,
} from '@mui/material'
import CloudOutlinedIcon from '@mui/icons-material/CloudOutlined'
import MemoryOutlinedIcon from '@mui/icons-material/MemoryOutlined'
import CheckCircleIcon from '@mui/icons-material/CheckCircle'
import InfoOutlinedIcon from '@mui/icons-material/InfoOutlined'
import RefreshIcon from '@mui/icons-material/Refresh'
import SaveOutlinedIcon from '@mui/icons-material/SaveOutlined'
import TuneOutlinedIcon from '@mui/icons-material/TuneOutlined'
import { getProviderSettings, patchProviderSetting } from '../api'

/** 引擎图标：cloud 走云端，其余都是跑在本机的引擎（内置 / ComfyUI）。 */
const engineIcon = (name) => (name === 'cloud' ? CloudOutlinedIcon : MemoryOutlinedIcon)

/** 掩码字符：后端回显已保存的密钥时只给后 4 位，带掩码的值不参与回传。 */
const MASK_CHAR = '\u2022'

export default function Settings() {
  const [data, setData] = useState(null)
  const [draft, setDraft] = useState({})
  const [saving, setSaving] = useState('')
  const [msg, setMsg] = useState(null)

  const load = async () => {
    try {
      setData(await getProviderSettings())
    } catch (e) {
      setMsg({ type: 'error', text: `加载设置失败：${e?.response?.data?.detail || e.message}` })
    }
  }

  useEffect(() => {
    load()
  }, [])

  // 数据到达后，用「当前生效的引擎」初始化每个分组草稿
  useEffect(() => {
    if (!data?.groups) return
    const next = {}
    for (const g of data.groups) {
      const cur = g.options.find((o) => o.name === g.current) || g.options[0]
      next[g.kind] = {
        name: cur?.name || '',
        model: cur?.model || '',
        credentials: Object.fromEntries((cur?.fields || []).map((f) => [f.key, f.value || ''])),
        makeDefault: true,
      }
    }
    setDraft(next)
  }, [data])

  const pickEngine = (g, opt) =>
    setDraft((d) => ({
      ...d,
      [g.kind]: {
        name: opt.name,
        model: opt.model || '',
        credentials: Object.fromEntries((opt.fields || []).map((f) => [f.key, f.value || ''])),
        makeDefault: true,
      },
    }))

  const setField = (kind, key, value) =>
    setDraft((d) => ({ ...d, [kind]: { ...d[kind], credentials: { ...d[kind].credentials, [key]: value } } }))

  const save = async (g) => {
    const d = draft[g.kind]
    const opt = g.options.find((o) => o.name === d.name)
    if (!opt) return
    // 只回传用户真正改动过的凭证：仍是掩码的值必须跳过，
    // 否则会把 "••••1234" 当成新密钥存进库里。
    const credentials = {}
    for (const f of opt.fields || []) {
      const v = d.credentials[f.key]
      if (v === undefined) continue
      if (f.secret && v.startsWith(MASK_CHAR)) continue
      credentials[f.key] = v
    }
    const body = { kind: g.kind, name: d.name, model: d.model, make_default: d.makeDefault !== false }
    if (Object.keys(credentials).length) body.credentials = credentials

    setSaving(g.kind)
    setMsg(null)
    try {
      await patchProviderSetting(body)
      await load()
      setMsg({ type: 'success', text: `${g.label}已保存：引擎切到「${opt.display_name}」，立即生效，重启后保留。` })
    } catch (e) {
      setMsg({ type: 'error', text: `保存失败：${e?.response?.data?.detail || e.message}` })
    } finally {
      setSaving('')
    }
  }

  if (!data) {
    return (
      <Typography color="text.secondary" sx={{ py: 6, textAlign: 'center' }}>
        加载中…
      </Typography>
    )
  }

  return (
    <Box>
      <Stack direction="row" alignItems="flex-end" spacing={2} sx={{ mb: 0.5 }}>
        <Typography variant="h5" fontWeight={700}>
          系统设置
        </Typography>
        <Typography variant="body2" color="text.secondary" sx={{ pb: 0.4 }}>
          为每类生成任务选择引擎（本地 / 云端），并配置模型与连接信息
        </Typography>
        <Box sx={{ flex: 1 }} />
        <Button size="small" startIcon={<RefreshIcon />} onClick={load}>
          重新探测
        </Button>
      </Stack>

      <Divider sx={{ my: 2 }} />

      {msg && (
        <Alert severity={msg.type} sx={{ mb: 2 }} onClose={() => setMsg(null)}>
          {msg.text}
        </Alert>
      )}

      <Alert severity="info" icon={<InfoOutlinedIcon />} sx={{ mb: 2 }}>
        设置保存在数据库中，<b>改完立即生效、重启服务后依然保留</b>。
        云端引擎的 API Key 只以掩码回显（如 <code>••••1234</code>），不会明文返回浏览器；
        不动该输入框即保持原值，手动清空则删除已保存的密钥，直接输入新值即覆盖。
      </Alert>

      <Stack spacing={2.5}>
        {data.groups.map((g) => (
          <GroupCard
            key={g.kind}
            group={g}
            draft={draft[g.kind]}
            onPick={(opt) => pickEngine(g, opt)}
            onField={(key, value) => setField(g.kind, key, value)}
            onModel={(value) => setDraft((d) => ({ ...d, [g.kind]: { ...d[g.kind], model: value } }))}
            onMakeDefault={(v) => setDraft((d) => ({ ...d, [g.kind]: { ...d[g.kind], makeDefault: v } }))}
            onSave={() => save(g)}
            saving={saving === g.kind}
          />
        ))}
      </Stack>
    </Box>
  )
}

function GroupCard({ group, draft, onPick, onField, onModel, onMakeDefault, onSave, saving }) {
  if (!draft) return null
  const selected = group.options.find((o) => o.name === draft.name) || group.options[0]
  const needsCreds = (selected?.fields || []).length > 0
  const credsReady = (selected?.fields || [])
    .filter((f) => f.key === 'base_url')
    .every((f) => (draft.credentials[f.key] || '').trim())
  const blocked = needsCreds && !credsReady

  return (
    <Paper variant="outlined" sx={{ p: 2.5, borderRadius: 3 }}>
      <Stack direction="row" alignItems="center" spacing={1} sx={{ mb: 0.5 }}>
        <TuneOutlinedIcon fontSize="small" color="primary" />
        <Typography variant="subtitle1" fontWeight={700}>
          {group.label}
        </Typography>
        <Chip
          size="small"
          color={selected?.is_default ? 'primary' : 'default'}
          variant={selected?.is_default ? 'filled' : 'outlined'}
          label={selected?.is_default ? '当前生效' : '未生效'}
        />
        <Box sx={{ flex: 1 }} />
        <Typography variant="caption" color="text.disabled">
          {group.hint}
        </Typography>
      </Stack>
      <Divider sx={{ my: 1.5 }} />

      <Grid container spacing={1.5}>
        {group.options.map((opt) => {
          const Icon = engineIcon(opt.name)
          const active = opt.name === draft.name
          return (
            <Grid item xs={12} sm={6} md={4} key={opt.name}>
              <Box
                onClick={() => onPick(opt)}
                sx={{
                  p: 1.5,
                  borderRadius: 2.5,
                  cursor: 'pointer',
                  border: '1.5px solid',
                  borderColor: active ? 'primary.main' : 'divider',
                  bgcolor: active ? 'rgba(109,74,255,0.05)' : 'transparent',
                  transition: 'all .15s',
                  '&:hover': { borderColor: 'primary.light' },
                  height: '100%',
                }}
              >
                <Stack direction="row" spacing={1} alignItems="center">
                  <Icon fontSize="small" color={active ? 'primary' : 'disabled'} />
                  <Typography variant="body2" fontWeight={650}>
                    {opt.display_name}
                  </Typography>
                </Stack>
                <Stack direction="row" spacing={0.6} sx={{ mt: 1, flexWrap: 'wrap', gap: 0.6 }}>
                  <Chip
                    size="small"
                    variant="outlined"
                    color={opt.functional ? 'success' : 'default'}
                    label={opt.functional ? '可用' : '未就绪'}
                  />
                  {opt.model && <Chip size="small" variant="outlined" label={opt.model} />}
                </Stack>
                {opt.doc && (
                  <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 1 }}>
                    {opt.doc}
                  </Typography>
                )}
              </Box>
            </Grid>
          )
        })}
      </Grid>

      {selected && (
        <Box sx={{ mt: 2 }}>
          <Divider sx={{ mb: 2 }} />
          <Grid container spacing={1.5}>
            <Grid item xs={12} sm={6}>
              <TextField
                size="small"
                fullWidth
                label="模型名"
                placeholder="留空使用引擎默认，如 sdxl-base-1.0 / wan2.1 / cosyvoice-v2"
                value={draft.model}
                onChange={(e) => onModel(e.target.value)}
              />
            </Grid>
            {(selected.fields || []).map((f) => (
              <Grid item xs={12} sm={6} key={f.key}>
                <TextField
                  size="small"
                  fullWidth
                  label={f.label}
                  placeholder={f.placeholder || ''}
                  value={draft.credentials[f.key] || ''}
                  onChange={(e) => onField(f.key, e.target.value)}
                  helperText={
                    f.secret && f.configured && (draft.credentials[f.key] || '').startsWith(MASK_CHAR)
                      ? '已保存（显示为掩码）。留空不动即保持原值；直接输入新密钥可覆盖。'
                      : undefined
                  }
                />
              </Grid>
            ))}
          </Grid>

          {needsCreds && (
            <Alert severity={credsReady ? 'success' : 'warning'} sx={{ mt: 2 }} icon={credsReady ? <CheckCircleIcon /> : undefined}>
              {credsReady
                ? '连接信息已填写，保存后该引擎即可使用。'
                : '该引擎需要先填写「接口地址 / 服务地址」才能使用；现在保存会保留选择，但任务执行时会失败。'}
            </Alert>
          )}

          <Stack direction="row" spacing={2} alignItems="center" sx={{ mt: 2 }}>
            <Tooltip title="关闭后仅保存配置，不改变该类任务的默认引擎">
              <FormControlLabel
                control={
                  <Switch
                    size="small"
                    checked={draft.makeDefault !== false}
                    onChange={(e) => onMakeDefault(e.target.checked)}
                  />
                }
                label={<Typography variant="caption">设为默认引擎</Typography>}
              />
            </Tooltip>
            <Box sx={{ flex: 1 }} />
            <Button
              variant="contained"
              size="small"
              startIcon={<SaveOutlinedIcon />}
              onClick={onSave}
              disabled={saving}
            >
              {saving ? '保存中…' : '保存'}
            </Button>
          </Stack>
        </Box>
      )}
    </Paper>
  )
}
