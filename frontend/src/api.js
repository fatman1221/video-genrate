import axios from 'axios'
import { useCallback, useEffect, useRef, useState } from 'react'

const client = axios.create({ timeout: 60000 })

export const get = (url, params) => client.get(url, { params }).then((r) => r.data)
export const post = (url, body) => client.post(url, body).then((r) => r.data)

export const invokeSkill = (name, payload = {}, actor = 'agent') =>
  client
    .post(`/api/skills/${name}/invoke`, payload, { params: { actor } })
    .then((r) => r.data)

/** Web UI 里由人触发的操作，actor 记为 human，便于与 Agent 行为区分。 */
const invokeAsHuman = (name, payload = {}) => invokeSkill(name, payload, 'human')

export const SKILL = {
  bootstrap: (p) => invokeSkill('bootstrap_project', p),
  runPipeline: (p) => invokeSkill('run_pipeline', p),
  compose: (p) => invokeSkill('compose_video', p),
  quality: (p) => invokeSkill('run_quality_check', p),
  regenerateVideo: (p) => invokeSkill('regenerate_video', p),
  regenerateImage: (p) => invokeSkill('regenerate_image', p),
  regenerateVoice: (p) => invokeSkill('regenerate_voice', p),
  updateShot: (p) => invokeAsHuman('update_shot', p),
  mergeVideo: (p) => invokeSkill('merge_video', p),
  generateMusic: (p) => invokeSkill('generate_music', p),
  generateSubtitle: (p) => invokeSkill('generate_subtitle', p),
  generateVoice: (p) => invokeSkill('generate_voice', p),
  generateCharacterReference: (p) => invokeSkill('generate_character_reference', p),
  regenerateAsset: (p) => invokeSkill('regenerate_asset', p),
  retryTask: (p) => invokeSkill('retry_task', p),
  cancelTask: (p) => invokeSkill('cancel_task', p),
  resume: (p) => invokeSkill('resume_project', p),
  // 节点级流水线（人工操作，actor=human）
  rollbackStage: (p) => invokeAsHuman('rollback_stage', p),
  regenerateStage: (p) => invokeAsHuman('regenerate_stage', p),
  reviewStage: (p) => invokeAsHuman('review_stage', p),
  // 连续剧（系列 / 分集 / 系列级角色）
  createSeries: (p) => invokeAsHuman('create_series', p),
  createEpisode: (p) => invokeAsHuman('create_episode', p),
  promoteCharacter: (p) => invokeAsHuman('promote_character_to_series', p),
  deleteSeries: (p) => invokeAsHuman('delete_series', p),
  // 素材中心 / 系统设置
  generateStandaloneVoice: (p) => invokeAsHuman('generate_standalone_voice', p),
  setDefaultProvider: (p) => invokeAsHuman('set_default_provider', p),
}

// ---- 素材中心 ----
/** 跨项目聚合；project_id 留空即全部项目，unassigned=true 只看独立素材。 */
export const getAssetCenter = (params) => get('/api/assets', params)
/** 独立合成语音并入库（本地 TTS 通常几秒，云端可能较慢，故放宽超时）。 */
export const generateVoiceAsset = (body) =>
  client.post('/api/assets/generate/voice', body, { timeout: 300000 }).then((r) => r.data)

// ---- 系统设置（生图 / 图生视频 / 语音生成 三类模型）----
export const getProviderSettings = () => get('/api/settings/providers')
export const patchProviderSetting = (body) =>
  client.patch('/api/settings/providers', body).then((r) => r.data)
export const getTtsVoices = () => get('/api/settings/voices')

// ---- 连续剧（Series）----
export const getSeriesList = (params) => get('/api/series', params)
export const getSeries = (seriesId) => get(`/api/series/${seriesId}`)
export const getSeriesEpisodes = (seriesId) => get(`/api/series/${seriesId}/episodes`)
export const getSeriesCharacters = (seriesId) => get(`/api/series/${seriesId}/characters`)
export const patchSeries = (seriesId, body) =>
  client.patch(`/api/series/${seriesId}`, body).then((r) => r.data)
export const removeSeries = (seriesId, deleteEpisodes = false) =>
  client
    .delete(`/api/series/${seriesId}`, { params: { delete_episodes: deleteEpisodes } })
    .then((r) => r.data)

export const getPipeline = (projectId) => get(`/api/projects/${projectId}/pipeline`)

/** 项目历史上产出的全部成片（版本对比用，按时间倒序）。 */
export const getProjectOutputs = (projectId) => get(`/api/projects/${projectId}/outputs`)

export const deleteAsset = (assetId) =>
  fetch(`/api/assets/${assetId}?confirm=true`, { method: 'DELETE' }).then((r) => {
    if (!r.ok) throw new Error(`HTTP ${r.status}`)
    return r.json()
  })

/** 通用数据拉取 hook，支持轮询。 */
export function useApi(url, params, { poll = 0, deps = [] } = {}) {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(Boolean(url))
  const [error, setError] = useState(null)
  const timer = useRef(null)
  const paramsKey = JSON.stringify(params || {})

  const load = useCallback(async () => {
    if (!url) return
    try {
      const result = await get(url, JSON.parse(paramsKey))
      setData(result)
      setError(null)
    } catch (err) {
      setError(err?.response?.data?.detail || err.message)
    } finally {
      setLoading(false)
    }
  }, [url, paramsKey])

  useEffect(() => {
    setLoading(true)
    load()
    if (poll > 0) {
      timer.current = setInterval(load, poll)
    }
    return () => timer.current && clearInterval(timer.current)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [load, poll, ...deps])

  return { data, loading, error, reload: load }
}

/** SSE：实时日志与任务状态。 */
export function useProjectStream(projectId, handlers = {}) {
  const [connected, setConnected] = useState(false)
  const handlerRef = useRef(handlers)
  handlerRef.current = handlers

  useEffect(() => {
    if (!projectId) return undefined
    const source = new EventSource(`/api/projects/${projectId}/stream`)
    source.addEventListener('open', () => setConnected(true))
    source.addEventListener('logs', (e) => {
      try {
        handlerRef.current.onLogs?.(JSON.parse(e.data).logs)
      } catch (err) {
        /* ignore */
      }
    })
    source.addEventListener('tasks', (e) => {
      try {
        handlerRef.current.onTasks?.(JSON.parse(e.data).tasks)
      } catch (err) {
        /* ignore */
      }
    })
    source.addEventListener('error', () => setConnected(false))
    return () => {
      setConnected(false)
      source.close()
    }
  }, [projectId])

  return { connected }
}

export const fmtDuration = (seconds) => {
  const s = Number(seconds || 0)
  if (!s) return '—'
  const m = Math.floor(s / 60)
  const rest = (s % 60).toFixed(1)
  return m ? `${m}分${rest}秒` : `${rest}秒`
}

export const fmtBytes = (bytes) => {
  const b = Number(bytes || 0)
  if (!b) return '—'
  if (b < 1024) return `${b} B`
  if (b < 1024 * 1024) return `${(b / 1024).toFixed(1)} KB`
  return `${(b / 1024 / 1024).toFixed(2)} MB`
}

export const fmtTime = (iso) => {
  if (!iso) return '—'
  const d = new Date(iso)
  return `${d.getMonth() + 1}/${d.getDate()} ${String(d.getHours()).padStart(2, '0')}:${String(
    d.getMinutes(),
  ).padStart(2, '0')}:${String(d.getSeconds()).padStart(2, '0')}`
}

export const statusColor = (status) => {
  switch ((status || '').toUpperCase()) {
    case 'SUCCESS':
    case 'READY':
    case 'COMPLETED':
    case 'PASS':
      return 'success'
    case 'RUNNING':
    case 'GENERATING':
    case 'ACCEPTED':
      return 'primary'
    case 'FAILED':
    case 'ERROR':
      return 'error'
    case 'RETRYING':
    case 'WARN':
    case 'CANCELLED':
      return 'warning'
    case 'PENDING':
    case 'QUEUED':
      return 'default'
    default:
      return 'default'
  }
}
