import { useMemo, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import {
  Alert,
  Box,
  Button,
  Card,
  CardContent,
  Chip,
  Divider,
  Grid,
  IconButton,
  LinearProgress,
  Paper,
  Snackbar,
  Stack,
  Tab,
  Tabs,
  ToggleButton,
  ToggleButtonGroup,
  Tooltip,
  Typography,
} from '@mui/material'
import ArrowBackIcon from '@mui/icons-material/ArrowBack'
import RocketLaunchIcon from '@mui/icons-material/RocketLaunch'
import MovieCreationIcon from '@mui/icons-material/MovieCreation'
import FactCheckIcon from '@mui/icons-material/FactCheck'
import RefreshIcon from '@mui/icons-material/Refresh'
import AutoAwesomeMotionIcon from '@mui/icons-material/AutoAwesomeMotion'
import ApprovalOutlinedIcon from '@mui/icons-material/ApprovalOutlined'
import PipelineFlow from '../components/PipelineFlow'
import StagePanel from '../components/StagePanel'
import ShotGrid from '../components/ShotGrid'
import AssetGrid from '../components/AssetGrid'
import TaskTable from '../components/TaskTable'
import LogStream from '../components/LogStream'
import FinalPanel from '../components/FinalPanel'
import { SKILL, deleteAsset, fmtBytes, fmtDuration, fmtTime, statusColor, useApi, useProjectStream } from '../api'

const TABS = ['概览', '脚本', '分镜', '人物', '素材', '任务', '日志', '成片']

const ASSET_FILTERS = [
  { value: 'ALL', label: '全部' },
  { value: 'IMAGE', label: '图片' },
  { value: 'VIDEO', label: '视频' },
  { value: 'VOICE', label: '配音' },
  { value: 'MUSIC', label: '音乐' },
  { value: 'SFX', label: '音效' },
  { value: 'SUBTITLE', label: '字幕' },
]

const STATUS_LABEL = {
  DRAFT: '草稿',
  PLANNING: '规划中',
  GENERATING: '生成中',
  COMPLETED: '已完成',
  FAILED: '失败',
  PAUSED: '已暂停',
}

function BigRing({ value = 0, size = 88, stroke = 7 }) {
  const r = (size - stroke) / 2
  const c = 2 * Math.PI * r
  return (
    <Box sx={{ position: 'relative', width: size, height: size, flexShrink: 0 }}>
      <Box component="svg" width={size} height={size} sx={{ transform: 'rotate(-90deg)', display: 'block' }}>
        <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="rgba(26,23,38,0.07)" strokeWidth={stroke} />
        <circle
          cx={size / 2} cy={size / 2} r={r} fill="none" stroke="#6D4AFF" strokeWidth={stroke}
          strokeLinecap="round" strokeDasharray={c}
          strokeDashoffset={c * (1 - Math.min(value, 1))}
          style={{ transition: 'stroke-dashoffset .6s ease' }}
        />
      </Box>
      <Box
        sx={{
          position: 'absolute', inset: 0, display: 'flex', flexDirection: 'column',
          alignItems: 'center', justifyContent: 'center',
        }}
      >
        <Typography variant="h5" sx={{ lineHeight: 1, fontVariantNumeric: 'tabular-nums' }}>
          {Math.round(value * 100)}
        </Typography>
        <Typography variant="caption" color="text.disabled" sx={{ fontSize: 10 }}>
          完成度
        </Typography>
      </Box>
    </Box>
  )
}

function StatTile({ label, value, tone = 'default' }) {
  return (
    <Box
      sx={{
        px: 1.5, py: 1.1, borderRadius: 2, bgcolor: 'rgba(26,23,38,0.025)',
        border: '1px solid rgba(26,23,38,0.05)', minWidth: 74,
      }}
    >
      <Typography variant="caption" color="text.disabled" sx={{ display: 'block', fontSize: 10.5 }}>
        {label}
      </Typography>
      <Typography
        variant="h6"
        sx={{ fontSize: 17, color: tone === 'error' ? 'error.main' : 'text.primary' }}
      >
        {value ?? 0}
      </Typography>
    </Box>
  )
}

export default function ProjectDetail() {
  const { projectId } = useParams()
  const navigate = useNavigate()
  const [tab, setTab] = useState(0)
  const [busy, setBusy] = useState(false)
  const [busyShot, setBusyShot] = useState(null)
  const [liveLogs, setLiveLogs] = useState([])
  const [toast, setToast] = useState(null)
  const [assetFilter, setAssetFilter] = useState('ALL')
  const [stageKey, setStageKey] = useState(null)
  const [stageOpen, setStageOpen] = useState(false)

  const { data: overview, reload: reloadOverview } = useApi(
    `/api/projects/${projectId}/overview`, null, { poll: 3000 },
  )
  const { data: pipeline, reload: reloadPipeline } = useApi(
    `/api/projects/${projectId}/pipeline`, null, { poll: 3000 },
  )
  const { data: shotsData, reload: reloadShots } = useApi(
    `/api/projects/${projectId}/shots`, null, { poll: 5000 },
  )
  const { data: assetsData, reload: reloadAssets } = useApi(
    '/api/assets', { project_id: projectId, limit: 500 }, { poll: 8000 },
  )
  const { data: tasksData, reload: reloadTasks } = useApi(
    `/api/projects/${projectId}/tasks`, { limit: 300 }, { poll: 3000 },
  )
  const { data: charsData, reload: reloadChars } = useApi(
    `/api/projects/${projectId}/characters`, null, { poll: 15000 },
  )
  const { data: logsData } = useApi(`/api/projects/${projectId}/logs`, { limit: 400 }, { poll: 6000 })

  const { connected } = useProjectStream(projectId, {
    onLogs: (incoming) => {
      setLiveLogs((prev) => [...prev.slice(-500), ...incoming])
      reloadOverview()
      reloadPipeline()
      reloadTasks()
    },
    onTasks: () => {
      reloadTasks()
      reloadShots()
      reloadPipeline()
    },
  })

  const reloadAll = () => {
    reloadOverview(); reloadPipeline(); reloadShots()
    reloadAssets(); reloadTasks(); reloadChars()
  }

  const mergedLogs = useMemo(() => {
    const map = new Map()
    ;(logsData?.items || []).forEach((l) => map.set(l.id, l))
    liveLogs.forEach((l) => map.set(l.id, l))
    return [...map.values()].sort((a, b) => new Date(a.ts) - new Date(b.ts))
  }, [logsData, liveLogs])

  const project = overview?.project
  const status = overview?.status
  const shots = shotsData?.items || []
  const assets = assetsData?.items || []
  const tasks = tasksData?.items || []
  const characters = charsData?.items || []
  const nodes = pipeline?.nodes || []
  const selectedNode = nodes.find((n) => n.step_key === stageKey) || null

  const run = async (label, promise) => {
    setBusy(true)
    try {
      const res = await promise
      if (!res.ok) throw new Error(res.error)
      setToast({ type: 'success', text: `${label}已提交` })
      setTimeout(reloadAll, 500)
      return res
    } catch (err) {
      setToast({ type: 'error', text: `${label}失败：${err.message}` })
      return null
    } finally {
      setBusy(false)
    }
  }

  /* ----------------------------- 节点操作 ----------------------------- */
  const openStage = (node) => {
    setStageKey(node.step_key)
    setStageOpen(true)
  }

  const handleRollback = async (node, reason) => {
    const res = await run(`回退到「${node.label}」`,
      SKILL.rollbackStage({ project_id: projectId, step_key: node.step_key, reason: reason || 'Web UI 回退' }))
    if (res) setStageOpen(false)
  }

  const handleRegenerateStage = async (node, { reset, reason }) => {
    const res = await run(`重新生成「${node.label}」`,
      SKILL.regenerateStage({
        project_id: projectId, step_key: node.step_key,
        reset, auto_rollback: true, reason: reason || 'Web UI 重新生成',
      }))
    if (res) setStageOpen(false)
  }

  const handleReview = async (node, decision, comment, opts = {}) => {
    const res = await run(
      decision === 'APPROVED' ? '审核通过' : '驳回',
      SKILL.reviewStage({
        project_id: projectId, step_key: node.step_key, decision, comment,
        auto_rollback: Boolean(opts.autoRollback), regenerate: Boolean(opts.regenerate),
      }),
    )
    if (res) setStageOpen(false)
  }

  /* ----------------------------- 镜头 / 素材 ----------------------------- */
  const regenerateShot = async (shot, kind) => {
    setBusyShot(shot.shot_id)
    try {
      const fn = kind === 'image' ? SKILL.regenerateImage : SKILL.regenerateVideo
      const res = await fn({ shot_id: shot.shot_id, reason: 'Web UI 手动重生成' })
      if (!res.ok) throw new Error(res.error)
      setToast({ type: 'success', text: `${shot.code} 已重新入队` })
      setTimeout(reloadAll, 500)
    } catch (err) {
      setToast({ type: 'error', text: `重生成失败：${err.message}` })
    } finally {
      setBusyShot(null)
    }
  }

  const removeAsset = async (asset) => {
    setBusy(true)
    try {
      await deleteAsset(asset.asset_id)
      setToast({ type: 'success', text: `已删除：${asset.name}` })
      setTimeout(reloadAll, 300)
    } catch (err) {
      setToast({ type: 'error', text: `删除失败：${err.message}` })
    } finally {
      setBusy(false)
    }
  }

  const regenerateAsset = async (asset) => {
    await run('重新生成素材', SKILL.regenerateAsset({ asset_id: asset.asset_id }))
  }

  if (!project) return <LinearProgress />

  const progress = (status?.progress || 0) / 100
  const summary = pipeline?.summary || {}
  const filteredAssets = assetFilter === 'ALL' ? assets : assets.filter((a) => a.type === assetFilter)
  const currentLabel = nodes.find((n) => n.step_key === pipeline?.current)?.label

  return (
    <Box>
      {/* ------------------------------ 顶部 ------------------------------ */}
      <Stack direction="row" alignItems="center" spacing={1.5} sx={{ mb: 2 }}>
        <Tooltip title={project.series_id ? '返回所属连续剧' : '返回项目列表'}>
          <IconButton
            onClick={() => navigate(project.series_id ? `/series/${project.series_id}` : '/')}
            size="small"
            sx={{ bgcolor: '#fff', border: '1px solid rgba(26,23,38,0.07)' }}
          >
            <ArrowBackIcon fontSize="small" />
          </IconButton>
        </Tooltip>
        <Box sx={{ flex: 1, minWidth: 0 }}>
          <Stack direction="row" spacing={1} alignItems="center" sx={{ minWidth: 0 }}>
            <Typography variant="h5" noWrap>{project.name}</Typography>
            {project.episode_no > 0 && (
              <Chip size="small" color="primary" label={`第 ${project.episode_no} 集`} />
            )}
            {project.series_name && (
              <Chip
                size="small"
                variant="outlined"
                label={project.series_name}
                onClick={() => navigate(`/series/${project.series_id}`)}
                sx={{ cursor: 'pointer' }}
              />
            )}
          </Stack>
          <Stack direction="row" spacing={1} alignItems="center" sx={{ mt: 0.4 }}>
            <Typography variant="caption" color="text.secondary">
              {project.style} · {project.width}×{project.height} @{project.fps}fps · 目标 {fmtDuration(project.target_duration)}
            </Typography>
          </Stack>
        </Box>
        <Stack direction="row" spacing={1} alignItems="center">
          <Button
            variant="contained" startIcon={<RocketLaunchIcon />} disabled={busy}
            onClick={() => run('推进流水线', SKILL.runPipeline({ project_id: projectId }))}
          >
            推进流水线
          </Button>
          <Tooltip title="把全部镜头拼接并混入配音/配乐/字幕，产出最终成片">
            <Button
              variant="outlined" startIcon={<MovieCreationIcon />} disabled={busy}
              onClick={() => run('合成成片', SKILL.compose({ project_id: projectId, with_music: true, with_subtitle: true }))}
            >
              合成
            </Button>
          </Tooltip>
          <Tooltip title="完整性 / 一致性校验">
            <IconButton onClick={() => run('质量检查', SKILL.quality({ project_id: projectId, auto_repair: true }))} disabled={busy}>
              <FactCheckIcon />
            </IconButton>
          </Tooltip>
          <Tooltip title="刷新">
            <IconButton onClick={reloadAll}><RefreshIcon /></IconButton>
          </Tooltip>
        </Stack>
      </Stack>

      {/* --------------------------- 生产进度节点 --------------------------- */}
      <Card sx={{ mb: 2.5, overflow: 'visible' }}>
        <CardContent sx={{ p: 2.5 }}>
          <Stack direction={{ xs: 'column', xl: 'row' }} spacing={2.5} alignItems={{ xl: 'center' }}>
            <Stack direction="row" spacing={2} alignItems="center" sx={{ minWidth: 262 }}>
              <BigRing value={progress} />
              <Box>
                <Stack direction="row" spacing={0.8} alignItems="center">
                  <Chip size="small" color={statusColor(project.status)} label={STATUS_LABEL[project.status] || project.status} />
                  <Chip size="small" variant="outlined" label={project.workflow_state} />
                </Stack>
                <Typography variant="body2" color="text.secondary" sx={{ mt: 0.8 }}>
                  {currentLabel ? `当前节点 · ${currentLabel}` : '全部节点已完成'}
                </Typography>
                <Typography variant="caption" color="text.disabled">
                  已完成 {summary.success || 0}/{summary.total || 0} 节点
                  {summary.pending_review > 0 ? ` · ${summary.pending_review} 个待审核` : ''}
                </Typography>
              </Box>
            </Stack>

            <Box sx={{ flex: 1, minWidth: 0, borderLeft: { xl: '1px solid rgba(26,23,38,0.07)' }, pl: { xl: 2.5 } }}>
              <Stack direction="row" alignItems="center" justifyContent="space-between" sx={{ mb: 1 }}>
                <Typography variant="subtitle2" sx={{ fontSize: 13 }}>
                  生产流水线
                </Typography>
                <Typography variant="caption" color="text.disabled">
                  点击节点可查看产出 · 回退 · 重新生成 · 审核
                </Typography>
              </Stack>
              <PipelineFlow
                nodes={nodes}
                selectedKey={stageKey}
                onSelect={openStage}
              />
            </Box>
          </Stack>

          {status?.next_actions?.length > 0 && (
            <Stack direction="row" spacing={0.8} alignItems="center" sx={{ mt: 2, flexWrap: 'wrap', gap: 0.8 }}>
              <Typography variant="caption" color="text.disabled">
                建议下一步
              </Typography>
              {status.next_actions.slice(0, 4).map((action) => {
                const key = action.match(/（([a-z_]+)）/)?.[1]
                const node = nodes.find((n) => n.step_key === key)
                return (
                  <Chip
                    key={action}
                    size="small"
                    variant="outlined"
                    color="primary"
                    icon={<AutoAwesomeMotionIcon sx={{ fontSize: 14 }} />}
                    label={action.replace(/（[a-z_]+）/, '')}
                    onClick={() => node && openStage(node)}
                  />
                )
              })}
            </Stack>
          )}
        </CardContent>
      </Card>

      {/* ------------------------------ 标签页 ------------------------------ */}
      <Tabs
        value={tab}
        onChange={(e, v) => setTab(v)}
        variant="scrollable"
        scrollButtons={false}
        sx={{ mb: 2, minHeight: 44, borderBottom: '1px solid rgba(26,23,38,0.07)' }}
      >
        {TABS.map((label) => (
          <Tab key={label} label={label} sx={{ mr: 0.5 }} />
        ))}
      </Tabs>

      {/* 0 概览 */}
      {tab === 0 && (
        <Grid container spacing={2.5}>
          <Grid item xs={12} md={7}>
            <Card sx={{ height: '100%' }}>
              <CardContent>
                <Stack direction="row" alignItems="center" justifyContent="space-between" sx={{ mb: 1.5 }}>
                  <Typography variant="h6">最终成片</Typography>
                  {overview?.final_output ? (
                    <Chip size="small" color="success" label="已产出" />
                  ) : (
                    <Chip size="small" label="未合成" />
                  )}
                </Stack>
                {overview?.final_output ? (
                  <Box
                    component="video"
                    src={overview.final_output.url}
                    controls
                    sx={{ width: '100%', borderRadius: 2, bgcolor: '#000', maxHeight: 340 }}
                  />
                ) : (
                  <Box
                    sx={{
                      height: 200, borderRadius: 2, display: 'flex', alignItems: 'center',
                      justifyContent: 'center', bgcolor: 'rgba(26,23,38,0.025)',
                      border: '1px dashed rgba(26,23,38,0.12)',
                    }}
                  >
                    <Stack alignItems="center" spacing={1}>
                      <MovieCreationIcon sx={{ color: 'text.disabled', fontSize: 30 }} />
                      <Typography variant="body2" color="text.disabled">
                        推进流水线全部完成后即可合成
                      </Typography>
                      <Button size="small" variant="outlined" onClick={() => run('合成成片', SKILL.compose({ project_id: projectId }))}>
                        立即合成
                      </Button>
                    </Stack>
                  </Box>
                )}

                <Stack direction="row" spacing={1} sx={{ mt: 2, flexWrap: 'wrap', gap: 1 }}>
                  <StatTile label="场景" value={status?.counts?.scenes} />
                  <StatTile label="镜头" value={status?.counts?.shots} />
                  <StatTile label="角色" value={status?.counts?.characters} />
                  <StatTile label="素材" value={status?.counts?.assets} />
                  <StatTile label="失败任务" value={status?.counts?.tasks_failed} tone={status?.counts?.tasks_failed ? 'error' : 'default'} />
                  <StatTile label="占用" value={fmtBytes(assetsData?.stats?.total_size_bytes)} />
                </Stack>
              </CardContent>
            </Card>
          </Grid>

          <Grid item xs={12} md={5}>
            <Card sx={{ height: '100%' }}>
              <CardContent>
                <Stack direction="row" alignItems="center" justifyContent="space-between" sx={{ mb: 1.2 }}>
                  <Typography variant="h6">Agent 实时执行</Typography>
                  <Stack direction="row" spacing={0.6} alignItems="center">
                    <Box
                      sx={{
                        width: 7, height: 7, borderRadius: '50%',
                        bgcolor: connected ? 'success.main' : 'text.disabled',
                      }}
                    />
                    <Typography variant="caption" color="text.secondary">
                      {connected ? '实时' : '未连接'}
                    </Typography>
                  </Stack>
                </Stack>
                <LogStream logs={mergedLogs.slice(-18)} connected={connected} height={288} />
                {summary.pending_review > 0 && (
                  <>
                    <Divider sx={{ my: 1.5 }} />
                    <Stack direction="row" spacing={1} alignItems="center">
                      <ApprovalOutlinedIcon sx={{ fontSize: 18, color: 'warning.main' }} />
                      <Typography variant="caption" color="text.secondary">
                        {summary.pending_review} 个节点待审核，点击上方节点进行处理
                      </Typography>
                    </Stack>
                  </>
                )}
              </CardContent>
            </Card>
          </Grid>
        </Grid>
      )}

      {/* 1 脚本 */}
      {tab === 1 && (
        <Grid container spacing={2.5}>
          <Grid item xs={12} md={4}>
            <Card>
              <CardContent>
                <Typography variant="h6" sx={{ mb: 1.2 }}>分章大纲</Typography>
                <Typography variant="body2" color="text.secondary" sx={{ whiteSpace: 'pre-wrap' }}>
                  {overview?.script?.outline || '尚未生成脚本'}
                </Typography>
                {overview?.script && (
                  <Stack direction="row" spacing={0.8} sx={{ mt: 2 }}>
                    <Chip size="small" variant="outlined" label={`${overview.script.length} 字`} />
                    <Chip size="small" variant="outlined" label={overview.script.provider} />
                  </Stack>
                )}
              </CardContent>
            </Card>
          </Grid>
          <Grid item xs={12} md={8}>
            <Card>
              <CardContent>
                <Typography variant="h6" sx={{ mb: 1.2 }}>脚本正文</Typography>
                <Box
                  sx={{
                    maxHeight: 620, overflow: 'auto', whiteSpace: 'pre-wrap', fontSize: 13.5,
                    lineHeight: 2, color: 'text.primary',
                  }}
                >
                  {overview?.script?.content || '尚未生成脚本'}
                </Box>
              </CardContent>
            </Card>
          </Grid>
        </Grid>
      )}

      {/* 2 分镜 */}
      {tab === 2 && <ShotGrid shots={shots} onRegenerate={regenerateShot} busyId={busyShot} />}

      {/* 3 人物 */}
      {tab === 3 && (
        <Grid container spacing={2.5}>
          {characters.length === 0 && (
            <Grid item xs={12}>
              <Paper sx={{ py: 6, textAlign: 'center', border: '1px dashed rgba(26,23,38,0.12)' }}>
                <Typography color="text.secondary">还没有角色</Typography>
              </Paper>
            </Grid>
          )}
          {characters.map((c) => {
            const ref = assets.find((a) => a.asset_id === c.reference_asset_id)
            return (
              <Grid item xs={12} sm={6} lg={4} key={c.character_id}>
                <Card sx={{ height: '100%' }}>
                  <Box sx={{ aspectRatio: '4 / 3', bgcolor: '#F5F5F9', position: 'relative' }}>
                    {ref ? (
                      <Box component="img" src={ref.url} alt={c.name} sx={{ width: '100%', height: '100%', objectFit: 'cover' }} />
                    ) : (
                      <Stack alignItems="center" justifyContent="center" sx={{ height: '100%' }} spacing={1}>
                        <Typography variant="caption" color="text.disabled">尚无参考图</Typography>
                        <Button
                          size="small" variant="outlined" disabled={busy}
                          onClick={() => run('生成参考图', SKILL.generateCharacterReference({ character_id: c.character_id }))}
                        >
                          生成
                        </Button>
                      </Stack>
                    )}
                  </Box>
                  <CardContent sx={{ py: 1.8 }}>
                    <Stack direction="row" alignItems="center" justifyContent="space-between">
                      <Typography variant="h6">{c.name}</Typography>
                      <Chip size="small" label={c.role} color={statusColor(c.status)} />
                    </Stack>
                    <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>
                      {c.appearance}
                    </Typography>
                  </CardContent>
                </Card>
              </Grid>
            )
          })}
        </Grid>
      )}

      {/* 4 素材 */}
      {tab === 4 && (
        <Box>
          <ToggleButtonGroup
            size="small"
            exclusive
            value={assetFilter}
            onChange={(e, v) => v && setAssetFilter(v)}
            sx={{ mb: 2, flexWrap: 'wrap', gap: 0.8 }}
          >
            {ASSET_FILTERS.map((f) => (
              <ToggleButton key={f.value} value={f.value}>
                {f.label}
                <Typography component="span" variant="caption" sx={{ ml: 0.6, color: 'text.disabled' }}>
                  {f.value === 'ALL' ? assets.length : assets.filter((a) => a.type === f.value).length}
                </Typography>
              </ToggleButton>
            ))}
          </ToggleButtonGroup>
          <AssetGrid assets={filteredAssets} busy={busy} onDelete={removeAsset} onRegenerate={regenerateAsset} />
        </Box>
      )}

      {/* 5 任务 */}
      {tab === 5 && (
        <TaskTable
          tasks={tasks}
          onRetry={(t) => run('重新入队', SKILL.retryTask({ task_id: t.task_id, reset_attempts: true }))}
          onCancel={(t) => run('取消任务', SKILL.cancelTask({ task_id: t.task_id }))}
        />
      )}

      {/* 6 日志 */}
      {tab === 6 && <LogStream logs={mergedLogs} connected={connected} height={620} />}

      {/* 7 成片 */}
      {tab === 7 && (
        <FinalPanel
          asset={overview?.final_output}
          quality={overview?.quality}
          busy={busy}
          onCompose={() => run('合成成片', SKILL.compose({ project_id: projectId }))}
          onQuality={() => run('质量检查', SKILL.quality({ project_id: projectId, auto_repair: false }))}
        />
      )}

      {/* 节点操作面板 */}
      <StagePanel
        node={selectedNode}
        open={stageOpen && Boolean(selectedNode)}
        onClose={() => setStageOpen(false)}
        busy={busy}
        onRollback={handleRollback}
        onRegenerate={handleRegenerateStage}
        onReview={handleReview}
      />

      <Snackbar
        open={Boolean(toast)}
        autoHideDuration={4500}
        onClose={() => setToast(null)}
        anchorOrigin={{ vertical: 'bottom', horizontal: 'center' }}
      >
        {toast ? <Alert severity={toast.type}>{toast.text}</Alert> : undefined}
      </Snackbar>
    </Box>
  )
}
