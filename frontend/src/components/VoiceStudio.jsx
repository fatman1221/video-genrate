import { useEffect, useMemo, useState } from 'react'
import {
  Alert,
  Box,
  Button,
  Card,
  CardContent,
  Chip,
  Divider,
  FormControl,
  Grid,
  IconButton,
  InputLabel,
  MenuItem,
  Select,
  Slider,
  Stack,
  TextField,
  ToggleButton,
  ToggleButtonGroup,
  Tooltip,
  Typography,
  CircularProgress,
} from '@mui/material'
import RecordVoiceOverIcon from '@mui/icons-material/RecordVoiceOver'
import GraphicEqIcon from '@mui/icons-material/GraphicEq'
import MusicNoteIcon from '@mui/icons-material/MusicNote'
import PlayArrowIcon from '@mui/icons-material/PlayArrow'
import ReplayIcon from '@mui/icons-material/Replay'
import SaveOutlinedIcon from '@mui/icons-material/SaveOutlined'
import RefreshIcon from '@mui/icons-material/Refresh'
import MovieCreationIcon from '@mui/icons-material/MovieCreation'
import {
  SKILL, fmtDuration, generateVoiceAsset, getTtsVoices,
} from '../api'

/** 试听用的固定短句：换音色/指令时听同一句才可比。 */
const SAMPLE_TEXT = '来上海七百多天了，我到现在也没整明白，这大城市晚上和白天，到底哪嘎达更消停。'

/**
 * 配音 · BGM 调音台。
 *
 * 三块能力：
 * 1. 全局音色 —— 选引擎与音色、试听、一键套用到全部镜头
 * 2. 逐镜配音 —— 台词 / 音色 / 情感指令逐条编辑，保存入库，单条或批量重生成
 * 3. 配乐与混音 —— BGM 曲风切换与生成、音量、一键重合成成片
 */
export default function VoiceStudio({ projectId, shots = [], assets = [], busy, run, onRefresh, notify }) {
  const [cfg, setCfg] = useState(null)
  const [cfgState, setCfgState] = useState('loading') // loading | ready | error
  const [engineName, setEngineName] = useState('')
  const [globalSpeaker, setGlobalSpeaker] = useState('')
  const [globalInstruct, setGlobalInstruct] = useState('')
  const [edits, setEdits] = useState({})       // shot_id -> {voice_script, voice_speaker, voice_instruct}
  const [audition, setAudition] = useState(null) // {loading, url, error, label}
  const [musicStyle, setMusicStyle] = useState('pop')
  const [musicPrompt, setMusicPrompt] = useState('')
  const [musicVolume, setMusicVolume] = useState(0.16)
  const [voiceVolume, setVoiceVolume] = useState(1)

  const loadCfg = async (force = false) => {
    setCfgState('loading')
    try {
      const data = await getTtsVoices(force)
      setCfg(data)
      const engines = data?.engines || []
      const qwen = engines.find((e) => e.name === 'qwen3tts') || engines[0]
      setEngineName((prev) => prev || qwen?.name || '')
      setGlobalSpeaker((prev) => prev || qwen?.default_speaker || '')
      if (!musicPrompt) {
        setMusicPrompt(data?.music_styles?.find((s) => s.name === 'pop')?.desc || '')
      }
      setCfgState('ready')
    } catch (err) {
      console.error('[VoiceStudio] 音色清单读取失败', err)
      setCfg(null)
      setCfgState('error')
    }
  }

  useEffect(() => {
    loadCfg()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId])

  const engines = cfg?.engines || []
  const engine = useMemo(
    () => engines.find((e) => e.name === engineName) || null,
    [engines, engineName],
  )
  const speakers = engine?.speakers || []
  const musicStyles = cfg?.music_styles || []

  /** 下拉选项 = 引擎音色 + 镜头里已在用但清单里没有的音色（避免出现空选项告警）。 */
  const speakerOptions = useMemo(() => {
    const names = new Set(speakers.map((s) => s.name))
    const extra = []
    shots.forEach((s) => {
      const used = edits[s.shot_id]?.voice_speaker ?? s.voice_speaker
      if (used && !names.has(used)) {
        names.add(used)
        extra.push({ name: used, label: `${used}（当前使用）` })
      }
    })
    return [...speakers, ...extra]
  }, [speakers, shots, edits])

  const bgm = useMemo(
    () => assets.find((a) => a.type === 'MUSIC') || null,
    [assets],
  )

  /** 合并「已保存值」与「本次编辑」，UI 只读这里。 */
  const rows = useMemo(
    () => shots.map((s) => ({
      ...s,
      e_script: edits[s.shot_id]?.voice_script ?? s.voice_script ?? '',
      e_speaker: edits[s.shot_id]?.voice_speaker ?? s.voice_speaker ?? '',
      e_instruct: edits[s.shot_id]?.voice_instruct ?? s.voice_instruct ?? '',
    })),
    [shots, edits],
  )

  const dirty = Object.keys(edits).length
  const missingVoice = rows.filter((r) => !r.assets?.voice?.url).length

  const setEdit = (shotId, key, value) => {
    setEdits((prev) => {
      const shot = shots.find((s) => s.shot_id === shotId)
      const base = prev[shotId] || {
        voice_script: shot?.voice_script ?? '',
        voice_speaker: shot?.voice_speaker ?? '',
        voice_instruct: shot?.voice_instruct ?? '',
      }
      const next = { ...base, [key]: value }
      // 与已保存值完全一致时移除，避免误报「有未保存修改」
      const same = next.voice_script === (shot?.voice_script ?? '')
        && next.voice_speaker === (shot?.voice_speaker ?? '')
        && next.voice_instruct === (shot?.voice_instruct ?? '')
      const copy = { ...prev }
      if (same) delete copy[shotId]
      else copy[shotId] = next
      return copy
    })
  }

  const applyGlobalToAll = () => {
    if (!rows.length) return
    const next = {}
    rows.forEach((r) => {
      next[r.shot_id] = {
        voice_script: r.e_script,
        voice_speaker: globalSpeaker || r.e_speaker,
        voice_instruct: globalInstruct || r.e_instruct,
      }
    })
    setEdits(next)
  }

  const saveAll = async () => {
    const ids = Object.keys(edits)
    if (!ids.length) return
    let ok = 0
    for (const id of ids) {
      const e = edits[id]
      const res = await SKILL.updateShot({
        shot_id: id,
        voice_script: e.voice_script,
        voice_speaker: e.voice_speaker,
        voice_instruct: e.voice_instruct,
      })
      if (res?.ok) ok += 1
    }
    setEdits({})
    onRefresh?.()
    return ok
  }

  const auditionVoice = async () => {
    setAudition({ loading: true })
    try {
      const res = await generateVoiceAsset({
        text: SAMPLE_TEXT,
        provider: engineName || undefined,
        speaker: globalSpeaker || undefined,
        instruct: globalInstruct || undefined,
        name: `试听 ${globalSpeaker || engineName} ${Date.now() % 10000}`,
        project_id: projectId,
      })
      const url = res?.data?.url || res?.url
      setAudition({ url, label: `${globalSpeaker || engineName} · ${globalInstruct || '默认语气'}` })
    } catch (err) {
      setAudition({ error: err?.response?.data?.detail || err.message })
    }
  }

  const regenerateOne = (shot) => run(`重新合成 ${shot.code} 配音`, SKILL.regenerateVoice({
    shot_id: shot.shot_id,
    speaker: shot.e_speaker || globalSpeaker || undefined,
    instruct: shot.e_instruct || undefined,
    voice_engine: engineName || undefined,
  }))

  const regenerateAll = async () => {
    if (dirty) await saveAll()
    await run('批量重生成配音', SKILL.generateVoice({
      project_id: projectId, force: true,
      speaker: globalSpeaker || undefined,
      provider: engineName || undefined,
    }))
  }

  const generateBgm = () => run('生成背景音乐', SKILL.generateMusic({
    project_id: projectId,
    style: musicStyle,
    prompt: musicPrompt || undefined,
  }))

  const compose = (extra = {}) => run('合成成片', SKILL.compose({
    project_id: projectId,
    with_music: true,
    with_subtitle: true,
    music_volume: musicVolume,
    voice_volume: voiceVolume,
    ...extra,
  }))

  return (
    <Grid container spacing={2.5}>
      {/* ------------------------- 左：音色与逐镜配音 ------------------------- */}
      <Grid item xs={12} lg={8}>
        <Card sx={{ mb: 2.5 }}>
          <CardContent>
            <Stack direction="row" alignItems="center" spacing={1} sx={{ mb: 1.5 }}>
              <RecordVoiceOverIcon color="primary" />
              <Typography variant="h6" sx={{ flex: 1 }}>配音引擎与音色</Typography>
              {engine && (
                <Chip
                  size="small"
                  color={engine.supports_instruct ? 'primary' : 'default'}
                  variant="outlined"
                  label={engine.supports_instruct ? '支持情感指令' : '不支持情感指令'}
                />
              )}
            </Stack>

            {cfgState === 'loading' && !cfg && (
              <Stack direction="row" spacing={1.2} alignItems="center" sx={{ py: 1.5 }}>
                <CircularProgress size={16} />
                <Typography variant="body2" color="text.secondary">
                  正在读取本机音色清单（首次约需数秒）…
                </Typography>
              </Stack>
            )}

            {cfgState === 'error' && (
              <Alert
                severity="warning"
                action={
                  <Button color="inherit" size="small" onClick={() => loadCfg(true)}>
                    重试
                  </Button>
                }
              >
                读取音色清单失败，请确认后端在线。
              </Alert>
            )}

            {cfg && (
              <>
                <Grid container spacing={2}>
                  <Grid item xs={12} sm={4}>
                    <FormControl fullWidth size="small">
                      <InputLabel>配音引擎</InputLabel>
                      <Select
                        label="配音引擎"
                        value={engineName}
                        onChange={(e) => {
                          setEngineName(e.target.value)
                          const eng = engines.find((x) => x.name === e.target.value)
                          setGlobalSpeaker(eng?.default_speaker || '')
                        }}
                      >
                        {engines.map((e) => (
                          <MenuItem key={e.name} value={e.name}>
                            {e.label}
                            <Typography component="span" variant="caption" color="text.disabled" sx={{ ml: 1 }}>
                              {e.name}
                            </Typography>
                          </MenuItem>
                        ))}
                      </Select>
                    </FormControl>
                  </Grid>
                  <Grid item xs={12} sm={4}>
                    <FormControl fullWidth size="small">
                      <InputLabel>音色</InputLabel>
                      <Select
                        label="音色"
                        value={globalSpeaker}
                        onChange={(e) => setGlobalSpeaker(e.target.value)}
                      >
                        {speakers.map((s) => (
                          <MenuItem key={s.name} value={s.name}>
                            <Box>
                              <Typography variant="body2">{s.label || s.name}</Typography>
                              {s.desc && (
                                <Typography variant="caption" color="text.disabled" display="block">
                                  {s.desc}
                                </Typography>
                              )}
                            </Box>
                          </MenuItem>
                        ))}
                      </Select>
                    </FormControl>
                  </Grid>
                  <Grid item xs={12} sm={4}>
                    <Stack direction="row" spacing={1} alignItems="center" sx={{ height: '100%' }}>
                      <Button
                        variant="contained"
                        size="small"
                        startIcon={audition?.loading ? <CircularProgress size={14} color="inherit" /> : <PlayArrowIcon />}
                        disabled={audition?.loading || busy}
                        onClick={auditionVoice}
                      >
                        试听
                      </Button>
                      <Button variant="outlined" size="small" disabled={busy || !rows.length} onClick={applyGlobalToAll}>
                        套用到全部镜头
                      </Button>
                    </Stack>
                  </Grid>
                  <Grid item xs={12}>
                    <TextField
                      fullWidth
                      size="small"
                      label="全局情感指令（套用时写入每个镜头）"
                      placeholder="例：用东北口音唠嗑，活泼自然，声音明亮清晰"
                      value={globalInstruct}
                      onChange={(e) => setGlobalInstruct(e.target.value)}
                      disabled={!engine?.supports_instruct}
                      helperText={
                        engine?.supports_instruct
                          ? '自然语言描述语气/情绪/口音，逐镜还可单独微调'
                          : '当前引擎不支持情感指令（Qwen3-TTS 支持）'
                      }
                    />
                  </Grid>
                </Grid>

                {audition?.url && (
                  <Box sx={{ mt: 2 }}>
                    <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 0.5 }}>
                      <Chip size="small" color="success" label="试听" />
                      <Typography variant="caption" color="text.secondary">{audition.label}</Typography>
                    </Stack>
                    <Box component="audio" src={audition.url} controls autoPlay sx={{ width: '100%', height: 34 }} />
                  </Box>
                )}
                {audition?.error && (
                  <Alert severity="error" sx={{ mt: 2 }}>{audition.error}</Alert>
                )}
              </>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardContent>
            <Stack direction="row" alignItems="center" spacing={1} sx={{ mb: 1.2 }}>
              <GraphicEqIcon color="primary" />
              <Typography variant="h6" sx={{ flex: 1 }}>逐镜配音</Typography>
              <Typography variant="caption" color="text.disabled">
                共 {rows.length} 镜{missingVoice ? ` · ${missingVoice} 镜缺配音` : ''}
              </Typography>
            </Stack>
            <Divider sx={{ mb: 1.5 }} />

            <Stack direction="row" spacing={1} sx={{ mb: 2, flexWrap: 'wrap', gap: 1 }}>
              <Button
                variant="contained"
                size="small"
                startIcon={<SaveOutlinedIcon />}
                disabled={!dirty || busy}
                onClick={async () => {
                  const n = await saveAll()
                  if (n) notify?.('success', `已保存 ${n} 个镜头的配音设置`)
                  else notify?.('error', '保存失败，请查看日志')
                }}
              >
                保存修改{dirty ? `（${dirty}）` : ''}
              </Button>
              <Button
                variant="outlined"
                size="small"
                startIcon={<ReplayIcon />}
                disabled={busy || !rows.length}
                onClick={regenerateAll}
              >
                全部重生成配音
              </Button>
              <Button variant="text" size="small" startIcon={<RefreshIcon />} onClick={onRefresh}>
                刷新
              </Button>
            </Stack>

            {dirty > 0 && (
              <Alert severity="info" sx={{ mb: 1.5 }}>
                有 {dirty} 个镜头的修改未保存。保存后再点「全部重生成配音」即可一次性重做。
              </Alert>
            )}

            <Stack spacing={1.2}>
              {rows.map((s) => {
                const voice = s.assets?.voice
                const over = voice?.duration && s.duration && voice.duration > s.duration + 0.4
                return (
                  <Box
                    key={s.shot_id}
                    sx={{ p: 1.4, borderRadius: 2, border: '1px solid', borderColor: 'divider' }}
                  >
                    <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 1 }}>
                      <Chip size="small" color="primary" label={s.code} />
                      <Typography variant="caption" color="text.secondary">
                        镜头 {s.duration}s
                      </Typography>
                      {voice?.duration != null && (
                        <Chip
                          size="small"
                          color={over ? 'warning' : 'default'}
                          variant="outlined"
                          label={`配音 ${voice.duration.toFixed(1)}s${over ? ' · 超长' : ''}`}
                        />
                      )}
                      {edits[s.shot_id] && <Chip size="small" color="warning" label="未保存" />}
                      <Box sx={{ flex: 1 }} />
                      <Tooltip title="按当前设置重新合成这一条">
                        <span>
                          <IconButton size="small" disabled={busy} onClick={() => regenerateOne(s)}>
                            <ReplayIcon fontSize="small" />
                          </IconButton>
                        </span>
                      </Tooltip>
                    </Stack>

                    <Grid container spacing={1.2}>
                      <Grid item xs={12} md={7}>
                        <TextField
                          fullWidth
                          size="small"
                          multiline
                          minRows={2}
                          label="旁白台词"
                          value={s.e_script}
                          onChange={(e) => setEdit(s.shot_id, 'voice_script', e.target.value)}
                        />
                      </Grid>
                      <Grid item xs={12} md={5}>
                        <Stack spacing={1.2}>
                          <FormControl fullWidth size="small">
                            <InputLabel>音色</InputLabel>
                            <Select
                              label="音色"
                              value={s.e_speaker || ''}
                              onChange={(e) => setEdit(s.shot_id, 'voice_speaker', e.target.value)}
                            >
                              <MenuItem value="">
                                <em>用项目默认</em>
                              </MenuItem>
                              {speakerOptions.map((sp) => (
                                <MenuItem key={sp.name} value={sp.name}>{sp.label || sp.name}</MenuItem>
                              ))}
                            </Select>
                          </FormControl>
                          <TextField
                            fullWidth
                            size="small"
                            label="情感指令"
                            value={s.e_instruct}
                            onChange={(e) => setEdit(s.shot_id, 'voice_instruct', e.target.value)}
                            placeholder="像跟朋友聊天一样，语气活泼自然"
                          />
                        </Stack>
                      </Grid>
                    </Grid>

                    {voice?.url && (
                      <Box component="audio" src={voice.url} controls sx={{ width: '100%', height: 34, mt: 1 }} />
                    )}
                  </Box>
                )
              })}
              {!rows.length && (
                <Typography variant="body2" color="text.secondary" sx={{ py: 3, textAlign: 'center' }}>
                  分镜还没有镜头
                </Typography>
              )}
            </Stack>
          </CardContent>
        </Card>
      </Grid>

      {/* ------------------------- 右：配乐与混音 ------------------------- */}
      <Grid item xs={12} lg={4}>
        <Card sx={{ mb: 2.5 }}>
          <CardContent>
            <Stack direction="row" alignItems="center" spacing={1} sx={{ mb: 1.5 }}>
              <MusicNoteIcon color="primary" />
              <Typography variant="h6" sx={{ flex: 1 }}>背景音乐</Typography>
              {bgm && <Chip size="small" color="success" variant="outlined" label="已有" />}
            </Stack>

            <Typography variant="caption" color="text.secondary">曲风</Typography>
            <ToggleButtonGroup
              size="small"
              exclusive
              fullWidth
              value={musicStyle}
              onChange={(e, v) => v && setMusicStyle(v)}
              sx={{ mt: 0.6, mb: 1.5 }}
            >
              {musicStyles.map((s) => (
                <ToggleButton key={s.name} value={s.name}>
                  {s.label || s.name}
                </ToggleButton>
              ))}
              {!musicStyles.length && (
                <>
                  <ToggleButton value="pop">流行律动 108BPM</ToggleButton>
                  <ToggleButton value="warm">温暖钢琴 76BPM</ToggleButton>
                </>
              )}
            </ToggleButtonGroup>

            <TextField
              fullWidth
              size="small"
              multiline
              minRows={2}
              label="风格描述（可选）"
              value={musicPrompt}
              onChange={(e) => setMusicPrompt(e.target.value)}
              placeholder="温暖治愈的流行配乐，明亮的大调，轻快不抢戏"
            />

            <Stack direction="row" spacing={1} sx={{ mt: 1.5 }}>
              <Button
                variant="contained"
                size="small"
                startIcon={<MusicNoteIcon />}
                disabled={busy}
                onClick={generateBgm}
              >
                生成音乐
              </Button>
                <Button
                variant="outlined"
                size="small"
                startIcon={<MovieCreationIcon />}
                disabled={busy}
                onClick={() => compose()}
              >
                应用并重合成
              </Button>
            </Stack>

            {bgm?.url && (
              <Box sx={{ mt: 2 }}>
                <Typography variant="caption" color="text.secondary">
                  当前 BGM{bgm.duration ? ` · ${fmtDuration(bgm.duration)}` : ''}
                </Typography>
                <Box component="audio" src={bgm.url} controls sx={{ width: '100%', height: 34, mt: 0.6 }} />
              </Box>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardContent>
            <Typography variant="h6" sx={{ mb: 1.5 }}>混音与合成</Typography>

            <Stack spacing={2}>
              <Box>
                <Stack direction="row" justifyContent="space-between">
                  <Typography variant="caption" color="text.secondary">背景音乐音量</Typography>
                  <Typography variant="caption" fontWeight={600}>{musicVolume.toFixed(2)}</Typography>
                </Stack>
                <Slider
                  size="small"
                  min={0}
                  max={0.6}
                  step={0.02}
                  value={musicVolume}
                  onChange={(e, v) => setMusicVolume(v)}
                />
                <Typography variant="caption" color="text.disabled">
                  默认 0.16 · 越大越抢人声
                </Typography>
              </Box>

              <Box>
                <Stack direction="row" justifyContent="space-between">
                  <Typography variant="caption" color="text.secondary">人声音量</Typography>
                  <Typography variant="caption" fontWeight={600}>{voiceVolume.toFixed(2)}</Typography>
                </Stack>
                <Slider
                  size="small"
                  min={0.4}
                  max={1.6}
                  step={0.05}
                  value={voiceVolume}
                  onChange={(e, v) => setVoiceVolume(v)}
                />
              </Box>

              <Divider />

              <Button
                variant="contained"
                startIcon={<MovieCreationIcon />}
                disabled={busy}
                onClick={() => compose()}
              >
                一键重合成成片
              </Button>
              <Button
                variant="text"
                size="small"
                disabled={busy}
                onClick={() => compose({ with_music: false })}
              >
                只重合成（不用 BGM）
              </Button>
              <Alert severity="info" icon={false} sx={{ mt: 0.5 }}>
                <Typography variant="caption">
                  合成是异步任务，提交后可在「任务 / 日志」页观察进度；成片会作为新版本出现在「成片」页。
                </Typography>
              </Alert>
            </Stack>
          </CardContent>
        </Card>
      </Grid>
    </Grid>
  )
}
