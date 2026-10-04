import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Alert,
  Box,
  Button,
  Card,
  CardContent,
  Chip,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Divider,
  FormControl,
  Grid,
  IconButton,
  InputAdornment,
  InputLabel,
  List,
  ListItem,
  ListItemButton,
  ListItemText,
  MenuItem,
  Paper,
  Select,
  Stack,
  TextField,
  Tooltip,
  Typography,
} from '@mui/material'
import ArticleOutlinedIcon from '@mui/icons-material/ArticleOutlined'
import AddIcon from '@mui/icons-material/Add'
import AutoAwesomeIcon from '@mui/icons-material/AutoAwesome'
import AudiotrackOutlinedIcon from '@mui/icons-material/AudiotrackOutlined'
import CheckIcon from '@mui/icons-material/Check'
import CloseIcon from '@mui/icons-material/Close'
import ContentCopyIcon from '@mui/icons-material/ContentCopy'
import DeleteOutlineIcon from '@mui/icons-material/DeleteOutline'
import DescriptionIcon from '@mui/icons-material/Description'
import ExpandLessIcon from '@mui/icons-material/ExpandLess'
import ExpandMoreIcon from '@mui/icons-material/ExpandMore'
import ImageIcon from '@mui/icons-material/Image'
import MovieIcon from '@mui/icons-material/Movie'
import RefreshIcon from '@mui/icons-material/Refresh'
import SaveOutlinedIcon from '@mui/icons-material/SaveOutlined'
import UploadFileIcon from '@mui/icons-material/UploadFile'
import {
  SKILL,
  fmtDuration,
  getPlanPrompt,
  getReferences,
  getSectionPrompt,
  getScriptSections,
  removeReference,
  uploadReference,
} from '../api'

/** 中文旁白语速（字/秒）—— 与后端 script_sections.CHARS_PER_SECOND 保持一致。 */
const CHARS_PER_SECOND = 4.6

const KIND_META = {
  text: { label: '文本', icon: DescriptionIcon, color: 'primary' },
  image: { label: '图片', icon: ImageIcon, color: 'success' },
  video: { label: '视频', icon: MovieIcon, color: 'warning' },
  audio: { label: '音频', icon: AudiotrackOutlinedIcon, color: 'info' },
}

const fmtBytes = (n) => {
  if (!n) return ''
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / 1024 / 1024).toFixed(1)} MB`
}

/**
 * 脚本工作台。
 *
 * 三件事：
 * 1. 分幕写作 —— 按幕切分，逐幕生成与修改，不必一次写完整篇
 * 2. 上下文连贯 —— 每幕的「生成」会打包前文正文 + 参考素材，Agent 续写自然衔接
 * 3. 参考素材 —— 上传大纲/文案/参考图/样片，作为创作依据
 *
 * 生成引擎沿用「Agent 在环」：工作台负责把上下文整理好，内容由对话里的 Agent 产出后回填。
 */
export default function ScriptStudio({ projectId, script, busy, run, onRefresh, notify }) {
  const [sections, setSections] = useState([])
  const [refs, setRefs] = useState([])
  const [loading, setLoading] = useState(true)
  const [currentId, setCurrentId] = useState('')
  const [draft, setDraft] = useState(null)
  const [showFull, setShowFull] = useState(false)
  const [busyLocal, setBusyLocal] = useState(false)
  const [uploadNote, setUploadNote] = useState('')
  const [uploadKind, setUploadKind] = useState('')
  const [dialog, setDialog] = useState(null) // {mode, title, prompt, sectionId}
  const [pasteText, setPasteText] = useState('')
  const [copied, setCopied] = useState(false)
  const fileRef = useRef(null)

  const load = useCallback(async () => {
    try {
      const [sec, ref] = await Promise.all([
        getScriptSections(projectId),
        getReferences(projectId),
      ])
      setSections(sec.items || [])
      setRefs(ref.items || [])
      setCurrentId((prev) => (
        (sec.items || []).some((s) => s.section_id === prev)
          ? prev
          : (sec.items || [])[0]?.section_id || ''
      ))
    } catch (err) {
      console.error('[ScriptStudio] 加载失败', err)
      notify?.('error', `脚本工作台加载失败：${err.message}`)
    } finally {
      setLoading(false)
    }
  }, [projectId, notify])

  useEffect(() => { load() }, [load])

  const current = useMemo(
    () => sections.find((s) => s.section_id === currentId) || null,
    [sections, currentId],
  )

  const dirty = useMemo(() => {
    if (!current || !draft) return false
    return (
      draft.title !== (current.title || '')
      || draft.summary !== (current.summary || '')
      || draft.beat !== (current.beat || '')
      || draft.content !== (current.content || '')
      || Number(draft.target_duration || 0) !== Number(current.target_duration || 0)
    )
  }, [current, draft])

  // 草稿同步：切换幕、或该幕被外部改写（保存 / Agent 写回）时，把编辑区刷新一遍。
  // 「用户正在改且还没保存」时不覆盖，避免输入被后台刷新冲掉。
  const draftForRef = useRef('')
  const dirtyRef = useRef(false)
  dirtyRef.current = dirty

  useEffect(() => {
    if (!current) { setDraft(null); draftForRef.current = ''; return }
    const switching = draftForRef.current !== current.section_id
    if (!switching && dirtyRef.current) return
    draftForRef.current = current.section_id
    setDraft({
      title: current.title || '',
      summary: current.summary || '',
      beat: current.beat || '',
      content: current.content || '',
      target_duration: current.target_duration || 0,
    })
  }, [currentId, current?.section_id, current?.updated_at]) // eslint-disable-line react-hooks/exhaustive-deps

  const writtenCount = sections.filter((s) => (s.content || '').trim()).length
  const totalTarget = sections.reduce(
    (sum, s) => sum + (s.target_duration || 0), 0,
  )

  const targetCharsOf = (section) => {
    const dur = section.target_duration
      || (sections.length ? (script?.parameters?.target_duration || 300) / sections.length : 60)
    return Math.max(120, Math.round(dur * CHARS_PER_SECOND))
  }

  /* ----------------------------- 保存 ----------------------------- */
  const save = async () => {
    if (!current || !draft) return
    setBusyLocal(true)
    const res = await run(`保存「${draft.title || current.title}」`, SKILL.upsertScriptSection({
      project_id: projectId,
      section_id: current.section_id,
      title: draft.title,
      summary: draft.summary,
      beat: draft.beat,
      content: draft.content,
      target_duration: Number(draft.target_duration) || 0,
    }))
    setBusyLocal(false)
    if (res) { await load(); onRefresh?.() }
  }

  /* --------------------------- 生成 / 写回 --------------------------- */
  const openSectionPrompt = async (section) => {
    setBusyLocal(true)
    try {
      const data = await getSectionPrompt(projectId, section.section_id)
      setPasteText('')
      setCopied(false)
      setDialog({
        mode: 'section',
        title: `生成「${section.title}」`,
        prompt: data.prompt,
        sectionId: section.section_id,
        targetChars: data.target_chars,
        referenceCount: data.reference_count,
      })
    } catch (err) {
      notify?.('error', `取生成上下文失败：${err.message}`)
    } finally {
      setBusyLocal(false)
    }
  }

  const openPlanPrompt = async () => {
    setBusyLocal(true)
    try {
      const data = await getPlanPrompt(projectId)
      setPasteText('')
      setCopied(false)
      setDialog({ mode: 'plan', title: '规划分幕结构', prompt: data.prompt })
    } catch (err) {
      notify?.('error', `取规划提示词失败：${err.message}`)
    } finally {
      setBusyLocal(false)
    }
  }

  const copyPrompt = async () => {
    try {
      await navigator.clipboard.writeText(dialog?.prompt || '')
      setCopied(true)
      setTimeout(() => setCopied(false), 1800)
    } catch {
      notify?.('error', '复制失败，请手动选中文本复制')
    }
  }

  const writeBackSection = async () => {
    const text = pasteText.trim()
    if (!text) { notify?.('error', '请先粘贴 Agent 生成的正文'); return }
    const res = await run('写回这一幕', SKILL.upsertScriptSection({
      project_id: projectId,
      section_id: dialog.sectionId,
      content: text,
    }))
    if (res) { setDialog(null); await load(); onRefresh?.() }
  }

  const writeBackPlan = async () => {
    const text = pasteText.trim()
    if (!text) { notify?.('error', '请先粘贴分幕结构（JSON 数组）'); return }
    let items
    try {
      // 容忍 Markdown 代码块包裹
      const cleaned = text.replace(/^```(?:json)?/m, '').replace(/```\s*$/m, '').trim()
      items = JSON.parse(cleaned)
      if (!Array.isArray(items)) throw new Error('顶层需要是数组')
    } catch (err) {
      notify?.('error', `解析失败：${err.message}。需要形如 [{"title":"第一幕 · 落脚","summary":"...","target_duration":60}]`)
      return
    }
    const res = await run('规划分幕', SKILL.planScriptSections({ project_id: projectId, sections: items }))
    if (res) { setDialog(null); await load(); onRefresh?.() }
  }

  /* ----------------------------- 幕操作 ----------------------------- */
  const addSection = async () => {
    const seq = sections.length + 1
    const res = await run(`新增第 ${seq} 幕`, SKILL.upsertScriptSection({
      project_id: projectId,
      sequence: seq,
      title: `第 ${seq} 幕`,
      target_duration: 60,
    }))
    if (res) await load()
  }

  const removeSection = async (section) => {
    const res = await run(`删除「${section.title}」`,
      SKILL.deleteScriptSection({ project_id: projectId, section_id: section.section_id }))
    if (res) await load()
  }

  /* ----------------------------- 素材 ----------------------------- */
  const pickFile = () => fileRef.current?.click()

  const onFilePicked = async (event) => {
    const file = event.target.files?.[0]
    event.target.value = ''
    if (!file) return
    setBusyLocal(true)
    try {
      await uploadReference(projectId, file, { kind: uploadKind, note: uploadNote })
      notify?.('success', `已上传参考素材「${file.name}」`)
      setUploadNote('')
      await load()
    } catch (err) {
      const detail = err?.response?.data?.detail || err.message
      notify?.('error', `上传失败：${detail}`)
    } finally {
      setBusyLocal(false)
    }
  }

  const dropReference = async (asset) => {
    try {
      await removeReference(projectId, asset.asset_id)
      notify?.('success', `已删除「${asset.name}」`)
      await load()
    } catch (err) {
      notify?.('error', `删除失败：${err.message}`)
    }
  }

  /* ----------------------------- 渲染 ----------------------------- */
  if (loading) {
    return (
      <Stack direction="row" spacing={1.5} alignItems="center" sx={{ py: 6, justifyContent: 'center' }}>
        <CircularProgress size={18} />
        <Typography variant="body2" color="text.secondary">正在读取脚本与参考资料…</Typography>
      </Stack>
    )
  }

  return (
    <>
      <Grid container spacing={2.5}>
        {/* ---------------------------- 左：幕列表 ---------------------------- */}
        <Grid item xs={12} lg={3}>
          <Card>
            <CardContent sx={{ pb: 1 }}>
              <Stack direction="row" alignItems="center" spacing={1}>
                <ArticleOutlinedIcon color="primary" fontSize="small" />
                <Typography variant="h6" sx={{ flex: 1 }}>分幕</Typography>
                {sections.length > 0 && (
                  <Chip size="small" variant="outlined"
                        label={`${writtenCount}/${sections.length} 已写`} />
                )}
              </Stack>
              {totalTarget > 0 && (
                <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 0.5 }}>
                  分段合计 {fmtDuration(totalTarget)}
                </Typography>
              )}
            </CardContent>
            <List dense sx={{ py: 0, maxHeight: 460, overflow: 'auto' }}>
              {sections.map((s) => {
                const written = !!(s.content || '').trim()
                return (
                  <ListItem key={s.section_id} disablePadding
                            secondaryAction={
                              <Tooltip title="删除这一幕">
                                <IconButton edge="end" size="small" disabled={busy || busyLocal}
                                            onClick={() => removeSection(s)}>
                                  <DeleteOutlineIcon fontSize="inherit" />
                                </IconButton>
                              </Tooltip>
                            }>
                    <ListItemButton selected={s.section_id === currentId}
                                    onClick={() => setCurrentId(s.section_id)}>
                      <ListItemText
                        primary={
                          <Stack direction="row" spacing={0.8} alignItems="center">
                            <Typography variant="body2" sx={{ fontWeight: 600 }}>
                              {s.sequence}. {s.title}
                            </Typography>
                            {written
                              ? <CheckIcon sx={{ fontSize: 14, color: 'success.main' }} />
                              : <Chip size="small" variant="outlined" label="待写"
                                      sx={{ height: 18, fontSize: 10 }} />}
                          </Stack>
                        }
                        secondary={`${(s.content || '').length} 字 · ${s.target_duration || '-'}s`}
                      />
                    </ListItemButton>
                  </ListItem>
                )
              })}
              {!sections.length && (
                <Box sx={{ px: 2, py: 2 }}>
                  <Typography variant="body2" color="text.secondary">
                    还没有分幕。可以先让 Agent 规划结构，或手动添加第一幕。
                  </Typography>
                </Box>
              )}
            </List>
            <Divider />
            <Stack spacing={1} sx={{ p: 1.5 }}>
              <Button size="small" startIcon={<AutoAwesomeIcon />} disabled={busy || busyLocal}
                      onClick={openPlanPrompt}>
                {sections.length ? '重新规划分幕' : '让 Agent 规划分幕'}
              </Button>
              <Button size="small" startIcon={<AddIcon />} disabled={busy || busyLocal}
                      onClick={addSection}>
                手动添加一幕
              </Button>
            </Stack>
          </Card>
        </Grid>

        {/* ---------------------------- 中：当前幕 ---------------------------- */}
        <Grid item xs={12} lg={6}>
          <Card>
            <CardContent>
              {!current && (
                <Box sx={{ py: 8, textAlign: 'center' }}>
                  <ArticleOutlinedIcon sx={{ fontSize: 40, color: 'text.disabled' }} />
                  <Typography color="text.secondary" sx={{ mt: 1.5 }}>
                    左侧还没有分幕
                  </Typography>
                  <Typography variant="body2" color="text.disabled" sx={{ mt: 0.5 }}>
                    点「让 Agent 规划分幕」拿到提示词交给 Agent，或手动添加一幕
                  </Typography>
                </Box>
              )}

              {current && draft && (
                <>
                  <Stack direction="row" alignItems="center" spacing={1} sx={{ mb: 1.5 }}>
                    <Typography variant="h6" sx={{ flex: 1 }}>
                      第 {current.sequence} 幕
                    </Typography>
                    {dirty && <Chip size="small" color="warning" label="未保存" />}
                    <Chip size="small" variant="outlined"
                          label={`${(draft.content || '').length} / 约 ${targetCharsOf(current)} 字`}
                          color={(draft.content || '').length > targetCharsOf(current) * 1.25
                            ? 'warning' : 'default'} />
                  </Stack>

                  <Grid container spacing={1.5}>
                    <Grid item xs={12} sm={7}>
                      <TextField fullWidth size="small" label="幕标题"
                                 value={draft.title}
                                 onChange={(e) => setDraft({ ...draft, title: e.target.value })} />
                    </Grid>
                    <Grid item xs={12} sm={5}>
                      <TextField fullWidth size="small" type="number" label="目标时长（秒）"
                                 value={draft.target_duration}
                                 onChange={(e) => setDraft({ ...draft, target_duration: e.target.value })}
                                 InputProps={{ endAdornment: <InputAdornment position="end">s</InputAdornment> }} />
                    </Grid>
                    <Grid item xs={12}>
                      <TextField fullWidth size="small" multiline minRows={2} label="这一幕要讲什么（要点）"
                                 helperText="生成正文的主要依据；写清楚剧情推进和要交代的信息"
                                 value={draft.summary}
                                 onChange={(e) => setDraft({ ...draft, summary: e.target.value })} />
                    </Grid>
                    <Grid item xs={12}>
                      <TextField fullWidth size="small" label="情绪节拍 / 关键转折"
                                 value={draft.beat}
                                 onChange={(e) => setDraft({ ...draft, beat: e.target.value })} />
                    </Grid>
                    <Grid item xs={12}>
                      <TextField fullWidth multiline minRows={10} label="正文（旁白 / 对白）"
                                 value={draft.content}
                                 onChange={(e) => setDraft({ ...draft, content: e.target.value })} />
                    </Grid>
                  </Grid>

                  <Stack direction="row" spacing={1} sx={{ mt: 2 }}>
                    <Button variant="contained" size="small" startIcon={<SaveOutlinedIcon />}
                            disabled={!dirty || busy || busyLocal} onClick={save}>
                      保存
                    </Button>
                    <Button variant="outlined" size="small" startIcon={<AutoAwesomeIcon />}
                            disabled={busy || busyLocal} onClick={() => openSectionPrompt(current)}>
                      生成这一段
                    </Button>
                    <Button variant="text" size="small" startIcon={<RefreshIcon />}
                            disabled={busy || busyLocal}
                            onClick={() => { setCurrentId(current.section_id); load() }}>
                      刷新
                    </Button>
                  </Stack>
                </>
              )}
            </CardContent>
          </Card>

          {/* 整篇预览 */}
          <Card sx={{ mt: 2.5 }}>
            <CardContent>
              <Stack direction="row" alignItems="center" spacing={1}>
                <DescriptionIcon fontSize="small" color="primary" />
                <Typography variant="h6" sx={{ flex: 1 }}>整篇脚本</Typography>
                {script && (
                  <Chip size="small" variant="outlined" label={`${script.length || 0} 字`} />
                )}
                <IconButton size="small" onClick={() => setShowFull((v) => !v)}>
                  {showFull ? <ExpandLessIcon /> : <ExpandMoreIcon />}
                </IconButton>
              </Stack>
              {showFull && (
                <>
                  <Divider sx={{ my: 1.5 }} />
                  <Box sx={{
                    maxHeight: 420, overflow: 'auto', whiteSpace: 'pre-wrap',
                    fontSize: 13.5, lineHeight: 1.95,
                  }}>
                    {script?.content || '还没有正文。逐幕写好后会自动拼成整篇。'}
                  </Box>
                </>
              )}
            </CardContent>
          </Card>
        </Grid>

        {/* --------------------------- 右：参考资料 --------------------------- */}
        <Grid item xs={12} lg={3}>
          <Card>
            <CardContent>
              <Stack direction="row" alignItems="center" spacing={1} sx={{ mb: 1 }}>
                <UploadFileIcon fontSize="small" color="primary" />
                <Typography variant="h6" sx={{ flex: 1 }}>参考资料</Typography>
                <Chip size="small" variant="outlined" label={refs.length} />
              </Stack>
              <Alert severity="info" icon={false} sx={{ mb: 1.5, py: 0.4 }}>
                <Typography variant="caption">
                  上传的素材会进入每一幕的生成上下文（文本带节选），用来对齐调性与设定。
                </Typography>
              </Alert>

              <Stack spacing={1}>
                <TextField size="small" fullWidth label="这份素材的用途（可选）"
                           placeholder="例：整体调性与禁忌"
                           value={uploadNote}
                           onChange={(e) => setUploadNote(e.target.value)} />
                <FormControl fullWidth size="small">
                  <InputLabel>类型</InputLabel>
                  <Select label="类型" value={uploadKind}
                          onChange={(e) => setUploadKind(e.target.value)}>
                    <MenuItem value="">自动判断</MenuItem>
                    <MenuItem value="text">文本（大纲/小说/文案）</MenuItem>
                    <MenuItem value="image">图片（角色/场景参考）</MenuItem>
                    <MenuItem value="video">视频（样片/参考成片）</MenuItem>
                    <MenuItem value="audio">音频（配音小样）</MenuItem>
                  </Select>
                </FormControl>
                <Button variant="outlined" size="small" startIcon={<UploadFileIcon />}
                        disabled={busyLocal} onClick={pickFile}>
                  选择文件上传
                </Button>
                <input ref={fileRef} type="file" hidden onChange={onFilePicked} />
              </Stack>

              <Divider sx={{ my: 1.5 }} />

              <List dense sx={{ py: 0 }}>
                {refs.map((a) => {
                  const kind = a.extra?.kind || 'text'
                  const meta = KIND_META[kind] || KIND_META.text
                  const Icon = meta.icon
                  return (
                    <ListItem key={a.asset_id} disablePadding
                              secondaryAction={
                                <IconButton edge="end" size="small" onClick={() => dropReference(a)}>
                                  <CloseIcon fontSize="inherit" />
                                </IconButton>
                              }>
                      <ListItemText
                        primary={
                          <Stack direction="row" spacing={0.6} alignItems="center">
                            <Icon sx={{ fontSize: 15, color: `${meta.color}.main` }} />
                            <Typography variant="body2" noWrap sx={{ maxWidth: 130 }}>
                              {a.name}
                            </Typography>
                          </Stack>
                        }
                        secondary={
                          [
                            meta.label,
                            a.duration ? fmtDuration(a.duration) : '',
                            a.size_bytes ? fmtBytes(a.size_bytes) : '',
                            a.extra?.note || '',
                          ].filter(Boolean).join(' · ')
                        }
                      />
                    </ListItem>
                  )
                })}
                {!refs.length && (
                  <Typography variant="body2" color="text.disabled" sx={{ py: 2, textAlign: 'center' }}>
                    还没有参考资料
                  </Typography>
                )}
              </List>
            </CardContent>
          </Card>
        </Grid>
      </Grid>

      {/* ---------------------------- 提示词弹窗 ---------------------------- */}
      <Dialog open={!!dialog} onClose={() => setDialog(null)} maxWidth="md" fullWidth>
        <DialogTitle sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
          <AutoAwesomeIcon fontSize="small" color="primary" />
          <Box sx={{ flex: 1 }}>{dialog?.title}</Box>
          <IconButton size="small" onClick={() => setDialog(null)}>
            <CloseIcon fontSize="small" />
          </IconButton>
        </DialogTitle>
        <DialogContent dividers>
          <Alert severity="info" sx={{ mb: 2 }}>
            {dialog?.mode === 'section' ? (
              <>
                下面这段提示词已经带上<strong>项目需求、全片分幕、前面已定稿的正文</strong>和
                <strong>参考资料</strong>。
                复制给对话里的 Agent 生成正文（约 {dialog?.targetChars} 字），再粘到下面的框里写回，
                续写就会自然承接前文。
              </>
            ) : (
              <>
                复制给 Agent，让它只规划<strong>分幕结构</strong>（不写正文）。
                Agent 会调用 Skill 直接写回；若它只给了 JSON，也可以粘到下面框里写回。
              </>
            )}
          </Alert>

          <Stack direction="row" spacing={1} sx={{ mb: 1 }} alignItems="center">
            <Typography variant="subtitle2" sx={{ flex: 1 }}>① 提示词</Typography>
            <Button size="small" variant="outlined"
                    startIcon={copied ? <CheckIcon /> : <ContentCopyIcon />}
                    onClick={copyPrompt}>
              {copied ? '已复制' : '复制'}
            </Button>
          </Stack>
          <Paper variant="outlined" sx={{
            p: 1.5, maxHeight: 300, overflow: 'auto', whiteSpace: 'pre-wrap',
            fontSize: 12.5, lineHeight: 1.8, bgcolor: 'action.hover',
          }}>
            {dialog?.prompt || ''}
          </Paper>

          <Typography variant="subtitle2" sx={{ mt: 2.5, mb: 1 }}>
            {dialog?.mode === 'section' ? '② 把 Agent 写好的正文粘这里' : '② 把分幕结构（JSON）粘这里'}
          </Typography>
          <TextField
            fullWidth
            multiline
            minRows={dialog?.mode === 'section' ? 8 : 5}
            value={pasteText}
            onChange={(e) => setPasteText(e.target.value)}
            placeholder={dialog?.mode === 'section'
              ? '直接粘贴正文，不要带「第几幕」之类标记'
              : '[{"title":"第一幕 · 落脚","summary":"...","beat":"...","target_duration":60}, ...]'}
          />
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setDialog(null)}>取消</Button>
          <Button variant="contained" disabled={!pasteText.trim() || busy}
                  onClick={dialog?.mode === 'section' ? writeBackSection : writeBackPlan}>
            {dialog?.mode === 'section' ? '写回这一幕' : '写回分幕结构'}
          </Button>
        </DialogActions>
      </Dialog>
    </>
  )
}
