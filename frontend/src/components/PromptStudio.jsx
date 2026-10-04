import { useEffect, useMemo, useState } from 'react'
import {
  Alert,
  Box,
  Button,
  Card,
  CardContent,
  Chip,
  CircularProgress,
  Divider,
  FormControl,
  Grid,
  InputLabel,
  MenuItem,
  Paper,
  Select,
  Stack,
  ToggleButton,
  ToggleButtonGroup,
  Tooltip,
  Typography,
} from '@mui/material'
import AccountTreeOutlinedIcon from '@mui/icons-material/AccountTreeOutlined'
import HistoryOutlinedIcon from '@mui/icons-material/HistoryOutlined'
import LockOutlinedIcon from '@mui/icons-material/LockOutlined'
import WarningAmberOutlinedIcon from '@mui/icons-material/WarningAmberOutlined'
import ContentCopyIcon from '@mui/icons-material/ContentCopy'
import LineageChain from './LineageChain'
import { getAssetProvenance, getPromptDetail, getPromptProvenance, getPrompts, fmtTime } from '../api'

/**
 * 提示词与血缘查看器（只读）。
 *
 * 这一层在 UI 里此前是不可见的：新能力全走 API，人看不到「这句提示词是怎么来的」。
 * 本组件把三件事摊开：
 *
 * 1. **当前版本正文** —— 编译产物，不是手写的字符串；
 * 2. **历史版本（只增不改）** —— 可逐版切换查看，历史版本永远不被覆盖；
 * 3. **血缘链** —— 从提示词向上（编译输入快照）、从产物向上（完整链路），缺失节点显式标出。
 *
 * ⚠️ 刻意保持只读：重新编译是「改设定 → 产生新版本」的完整动作，
 * 由 Agent 执行；这里只提供可复制的指令，避免人在界面上一键改掉事实来源。
 */

const TYPE_FILTERS = [
  { value: '', label: '全部' },
  { value: 'image', label: '关键帧' },
  { value: 'video', label: '视频' },
]

function MetaRow({ k, v }) {
  return (
    <Stack direction="row" spacing={1}>
      <Typography variant="caption" color="text.secondary" sx={{ minWidth: 76 }}>
        {k}
      </Typography>
      <Typography variant="caption" sx={{ wordBreak: 'break-all' }}>
        {v === null || v === undefined || v === '' ? '—' : String(v)}
      </Typography>
    </Stack>
  )
}

function CodeBlock({ text, maxHeight = 260, empty }) {
  if (!text) {
    return (
      <Typography variant="caption" color="text.disabled">
        {empty || '—'}
      </Typography>
    )
  }
  return (
    <Box
      sx={{
        bgcolor: 'action.hover', p: 1.2, borderRadius: 1, maxHeight, overflow: 'auto',
        border: '1px solid', borderColor: 'divider',
      }}
    >
      <Typography variant="caption" component="pre" sx={{ m: 0, whiteSpace: 'pre-wrap', lineHeight: 1.7 }}>
        {text}
      </Typography>
    </Box>
  )
}

function SlotList({ slots }) {
  if (!slots?.length) {
    return (
      <Typography variant="caption" color="text.disabled">
        这一版没有参考图槽位（纯文本编译）
      </Typography>
    )
  }
  return (
    <Stack spacing={0.8}>
      {slots.map((s, i) => (
        <Paper key={i} variant="outlined" sx={{ p: 1 }}>
          <Stack direction="row" spacing={0.8} alignItems="center" sx={{ flexWrap: 'wrap', gap: 0.6 }}>
            <Chip size="small" label={s.kind || 'REF'} color={s.kind === 'PLAN' ? 'warning' : 'primary'} variant="outlined" />
            <Typography variant="caption" sx={{ fontWeight: 600 }}>
              {s.role || s.slot || '—'}
            </Typography>
            <Chip
              size="small"
              color={s.admission_status === 'ready' ? 'success' : 'warning'}
              label={s.admission_status || '未知'}
            />
            {s.kind === 'PLAN' && (
              <Typography variant="caption" color="warning.main">
                PLAN 态不可投产
              </Typography>
            )}
          </Stack>
          <Stack direction="row" spacing={2} sx={{ mt: 0.6, flexWrap: 'wrap', gap: 0.6 }}>
            <Typography variant="caption" color="text.secondary">
              可决定：{(s.may_control || []).join('、') || '—'}
            </Typography>
            <Typography variant="caption" color="text.secondary">
              绝不可决定：{(s.must_not_control || []).join('、') || '—'}
            </Typography>
          </Stack>
          {s.file_path && (
            <Typography variant="caption" color="text.disabled" noWrap sx={{ display: 'block', mt: 0.4 }}>
              {s.file_path}
            </Typography>
          )}
        </Paper>
      ))}
    </Stack>
  )
}

export default function PromptStudio({ projectId, assets = [], notify }) {
  const [type, setType] = useState('')
  const [list, setList] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [activeId, setActiveId] = useState('')
  const [detail, setDetail] = useState(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [versionNo, setVersionNo] = useState(null)
  const [prov, setProv] = useState(null)
  const [provLoading, setProvLoading] = useState(false)

  const [assetId, setAssetId] = useState('')
  const [assetProv, setAssetProv] = useState(null)
  const [assetProvLoading, setAssetProvLoading] = useState(false)

  const loadList = async () => {
    if (!projectId) return
    setLoading(true)
    setError('')
    try {
      const data = await getPrompts(projectId, type)
      setList(data)
      const first = data.prompts?.[0]?.id || ''
      setActiveId((prev) => (data.prompts?.some((p) => p.id === prev) ? prev : first))
    } catch (err) {
      setError(err?.response?.data?.detail || err.message)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    loadList()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId, type])

  useEffect(() => {
    if (!activeId) {
      setDetail(null)
      setProv(null)
      return
    }
    let alive = true
    setDetailLoading(true)
    setProv(null)
    getPromptDetail(activeId)
      .then((d) => {
        if (!alive) return
        setDetail(d)
        const current = d.prompt?.versions?.find((v) => v.id === d.prompt.current_version_id)
        setVersionNo(current?.version ?? d.prompt?.latest_version ?? null)
      })
      .catch((err) => alive && setError(err?.response?.data?.detail || err.message))
      .finally(() => alive && setDetailLoading(false))
    return () => {
      alive = false
    }
  }, [activeId])

  const version = useMemo(() => {
    const versions = detail?.prompt?.versions || []
    if (!versions.length) return null
    return versions.find((v) => v.version === versionNo) || versions[versions.length - 1]
  }, [detail, versionNo])

  const isCurrent = version && detail?.prompt?.current_version_id === version.id

  const copy = async (text, label) => {
    try {
      await navigator.clipboard.writeText(text)
      notify?.('success', `${label}已复制`)
    } catch (err) {
      notify?.('warning', '复制失败，请手动选中复制')
    }
  }

  const loadProv = async () => {
    if (!activeId) return
    setProvLoading(true)
    try {
      setProv(await getPromptProvenance(activeId))
    } catch (err) {
      notify?.('error', err?.response?.data?.detail || err.message)
    } finally {
      setProvLoading(false)
    }
  }

  const loadAssetProv = async (id) => {
    setAssetId(id)
    if (!id) {
      setAssetProv(null)
      return
    }
    setAssetProvLoading(true)
    try {
      setAssetProv(await getAssetProvenance(id, 1))
    } catch (err) {
      notify?.('error', err?.response?.data?.detail || err.message)
      setAssetProv(null)
    } finally {
      setAssetProvLoading(false)
    }
  }

  const staleList = (list?.prompts || []).filter((p) => p.stale)

  return (
    <Grid container spacing={2.5}>
      {/* ------------------------------ 左：列表 ------------------------------ */}
      <Grid item xs={12} md={4}>
        <Card>
          <CardContent sx={{ pb: 1.5 }}>
            <Stack direction="row" alignItems="center" justifyContent="space-between" sx={{ mb: 1.4 }}>
              <Typography variant="h6">提示词</Typography>
              <Button size="small" onClick={loadList} disabled={loading}>
                刷新
              </Button>
            </Stack>

            <ToggleButtonGroup
              size="small"
              exclusive
              value={type}
              onChange={(e, v) => v !== null && setType(v)}
              sx={{ mb: 1.4 }}
            >
              {TYPE_FILTERS.map((f) => (
                <ToggleButton key={f.value} value={f.value}>
                  {f.label}
                </ToggleButton>
              ))}
            </ToggleButtonGroup>

            <Stack direction="row" spacing={0.8} sx={{ mb: 1.2, flexWrap: 'wrap', gap: 0.6 }}>
              <Chip size="small" variant="outlined" label={`共 ${list?.count ?? 0} 条`} />
              {staleList.length > 0 && (
                <Chip
                  size="small"
                  color="warning"
                  icon={<WarningAmberOutlinedIcon />}
                  label={`${staleList.length} 条已过期`}
                />
              )}
            </Stack>

            {error && (
              <Alert severity="error" sx={{ mb: 1 }}>
                {error}
              </Alert>
            )}

            {loading && !list && (
              <Stack alignItems="center" sx={{ py: 3 }}>
                <CircularProgress size={22} />
              </Stack>
            )}

            {list && list.count === 0 && (
              <Typography variant="body2" color="text.secondary" sx={{ py: 3, textAlign: 'center' }}>
                这个项目还没有编译过提示词。
                <br />
                <Typography variant="caption" color="text.disabled">
                  编译由 Agent 调用 compile_image_prompt / compile_video_prompt 完成
                </Typography>
              </Typography>
            )}

            <Stack spacing={1} sx={{ maxHeight: 460, overflow: 'auto' }}>
              {(list?.prompts || []).map((p) => (
                <Paper
                  key={p.id}
                  variant="outlined"
                  onClick={() => setActiveId(p.id)}
                  sx={{
                    p: 1.2, cursor: 'pointer',
                    borderColor: p.id === activeId ? 'primary.main' : 'divider',
                    bgcolor: p.id === activeId ? 'action.selected' : 'transparent',
                  }}
                >
                  <Stack direction="row" alignItems="center" justifyContent="space-between" spacing={0.8}>
                    <Typography variant="body2" sx={{ fontWeight: 600 }} noWrap>
                      {p.code}
                    </Typography>
                    <Stack direction="row" spacing={0.5}>
                      <Chip size="small" variant="outlined" label={`v${p.latest_version}`} />
                      {p.stale && <Chip size="small" color="warning" label="过期" />}
                    </Stack>
                  </Stack>
                  <Typography variant="caption" color="text.secondary" noWrap sx={{ display: 'block' }}>
                    {p.name}
                  </Typography>
                  {p.stale && p.stale_reasons?.length > 0 && (
                    <Typography variant="caption" color="warning.main" sx={{ display: 'block' }} noWrap>
                      {p.stale_reasons[0]}
                    </Typography>
                  )}
                </Paper>
              ))}
            </Stack>
          </CardContent>
        </Card>

        {/* 产物血缘反查 */}
        <Card sx={{ mt: 2 }}>
          <CardContent>
            <Stack direction="row" spacing={0.8} alignItems="center" sx={{ mb: 1.2 }}>
              <AccountTreeOutlinedIcon sx={{ fontSize: 18, color: 'text.secondary' }} />
              <Typography variant="h6">产物血缘反查</Typography>
            </Stack>
            <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mb: 1.2 }}>
              挑一个素材，往上还原它是怎么来的
            </Typography>
            <FormControl fullWidth size="small">
              <InputLabel id="asset-lineage-label">选择素材</InputLabel>
              <Select
                labelId="asset-lineage-label"
                label="选择素材"
                value={assetId}
                onChange={(e) => loadAssetProv(e.target.value)}
              >
                <MenuItem value="">
                  <em>不选</em>
                </MenuItem>
                {assets.map((a) => (
                  <MenuItem key={a.asset_id} value={a.asset_id}>
                    {a.type} · {a.name}
                  </MenuItem>
                ))}
              </Select>
            </FormControl>
            {assets.length === 0 && (
              <Typography variant="caption" color="text.disabled" sx={{ display: 'block', mt: 1 }}>
                项目里还没有素材
              </Typography>
            )}
            {assetProvLoading && (
              <Stack alignItems="center" sx={{ py: 2 }}>
                <CircularProgress size={20} />
              </Stack>
            )}
            {assetProv && !assetProvLoading && (
              <Box sx={{ mt: 1.6 }}>
                <LineageChain data={assetProv} direction="asset" />
              </Box>
            )}
          </CardContent>
        </Card>
      </Grid>

      {/* ------------------------------ 右：详情 ------------------------------ */}
      <Grid item xs={12} md={8}>
        {!detail && (
          <Paper sx={{ py: 8, textAlign: 'center', border: '1px dashed', borderColor: 'divider' }}>
            <Typography color="text.secondary">从左侧选一条提示词</Typography>
          </Paper>
        )}

        {detail && (
          <Card>
            <CardContent>
              <Stack direction="row" alignItems="flex-start" justifyContent="space-between" spacing={1}>
                <Box sx={{ minWidth: 0 }}>
                  <Typography variant="h6" noWrap>
                    {detail.prompt.code}
                  </Typography>
                  <Typography variant="caption" color="text.secondary">
                    {detail.prompt.name} · {detail.prompt.type} · {detail.prompt.id}
                  </Typography>
                </Box>
                <Stack direction="row" spacing={0.6} alignItems="center">
                  <Tooltip title="重新编译会从当前设定生成新版本；这里只复制指令交 Agent 执行">
                    <Button
                      size="small"
                      variant="outlined"
                      startIcon={<ContentCopyIcon />}
                      onClick={() =>
                        copy(
                          `请对镜头 ${detail.prompt.shot_id} 重新编译 ${detail.prompt.type} 提示词`
                          + `（当前 ${detail.prompt.code} 已到 v${detail.prompt.latest_version}）。`
                          + `先看 check_prompt_staleness 的过期理由，确认要改的是设定还是镜头文字。`,
                          '重编译指令',
                        )
                      }
                    >
                      复制重编译指令
                    </Button>
                  </Tooltip>
                  <Button
                    size="small"
                    variant="contained"
                    startIcon={<AccountTreeOutlinedIcon />}
                    disabled={provLoading}
                    onClick={loadProv}
                  >
                    血缘
                  </Button>
                </Stack>
              </Stack>

              {/* stale 提示：这是"哪些镜头要重出"的答案 */}
              {detail.staleness?.stale ? (
                <Alert severity="warning" icon={<WarningAmberOutlinedIcon />} sx={{ mt: 1.6 }}>
                  <Typography variant="body2" sx={{ fontWeight: 600 }}>
                    当前版本已过期 —— 编译输入变了
                  </Typography>
                  <Box component="ul" sx={{ m: '6px 0 0', pl: 2.4 }}>
                    {detail.staleness.reasons.map((r, i) => (
                      <li key={i}>
                        <Typography variant="caption">{r}</Typography>
                      </li>
                    ))}
                  </Box>
                  {detail.staleness.checked?.length > 0 && (
                    <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 0.6 }}>
                      比对了：{detail.staleness.checked.join(' · ')}
                    </Typography>
                  )}
                </Alert>
              ) : (
                <Alert severity="success" sx={{ mt: 1.6 }}>
                  {detail.staleness?.note || '当前版本与设定一致，无需重出'}
                </Alert>
              )}

              <Divider sx={{ my: 2 }} />

              {/* 版本时间线：只增不改 */}
              <Stack direction="row" spacing={0.8} alignItems="center" sx={{ mb: 1.2, flexWrap: 'wrap', gap: 0.6 }}>
                <HistoryOutlinedIcon sx={{ fontSize: 18, color: 'text.secondary' }} />
                <Typography variant="subtitle2">版本历史</Typography>
                <Typography variant="caption" color="text.disabled">
                  只增不改：改设定只产生新版本，历史版本永远不被覆盖
                </Typography>
              </Stack>
              <ToggleButtonGroup
                size="small"
                exclusive
                value={version?.version ?? null}
                onChange={(e, v) => v !== null && setVersionNo(v)}
                sx={{ mb: 2, flexWrap: 'wrap', gap: 0.6 }}
              >
                {(detail.prompt.versions || []).map((v) => (
                  <ToggleButton key={v.id} value={v.version}>
                    v{v.version}
                    {v.status !== 'READY' && (
                      <Typography component="span" variant="caption" sx={{ ml: 0.6, color: 'text.disabled' }}>
                        {v.status}
                      </Typography>
                    )}
                  </ToggleButton>
                ))}
              </ToggleButtonGroup>

              {version && (
                <>
                  <Stack direction="row" spacing={0.8} alignItems="center" sx={{ mb: 1.4, flexWrap: 'wrap', gap: 0.6 }}>
                    <Typography variant="subtitle2">v{version.version}</Typography>
                    {isCurrent ? (
                      <Chip size="small" color="primary" label="当前版本" />
                    ) : (
                      <Chip size="small" variant="outlined" label="历史版本（生成时用的就是它）" />
                    )}
                    <Typography variant="caption" color="text.disabled">
                      {fmtTime(version.created_at)} · by {version.created_by}
                    </Typography>
                  </Stack>

                  <Grid container spacing={2}>
                    <Grid item xs={12} lg={7}>
                      <Typography variant="caption" color="text.secondary">
                        正向正文（八段式编译产物）
                      </Typography>
                      <Box sx={{ mt: 0.4 }}>
                        <CodeBlock text={version.compiled_prompt} maxHeight={300} />
                      </Box>
                      <Stack direction="row" spacing={1} sx={{ mt: 0.8 }}>
                        <Button
                          size="small"
                          startIcon={<ContentCopyIcon />}
                          onClick={() => copy(version.compiled_prompt || '', '正向正文')}
                          disabled={!version.compiled_prompt}
                        >
                          复制正文
                        </Button>
                      </Stack>
                    </Grid>

                    <Grid item xs={12} lg={5}>
                      <Stack spacing={0.8}>
                        <MetaRow k="Provider" v={version.provider} />
                        <MetaRow k="Model" v={version.model} />
                        <MetaRow k="档位" v={version.resolution} />
                        <MetaRow k="画幅" v={version.aspect_ratio} />
                        <MetaRow
                          k="尺寸"
                          v={version.width ? `${version.width}x${version.height}` : ''}
                        />
                        <MetaRow
                          k="Recipe"
                          v={version.recipe
                            ? `${version.recipe.renderer}@${version.recipe.version}`
                            : ''}
                        />
                        <MetaRow k="参数" v={JSON.stringify(version.parameters || {})} />
                        <MetaRow k="原始基线" v={version.raw_prompt} />
                      </Stack>

                      <Stack direction="row" spacing={0.8} alignItems="center" sx={{ mt: 2, mb: 0.6 }}>
                        <LockOutlinedIcon sx={{ fontSize: 16, color: 'text.secondary' }} />
                        <Typography variant="caption" color="text.secondary">
                          连续性锁（强制逐字注入，单独成段）
                        </Typography>
                      </Stack>
                      {version.continuity_lock_ids?.length > 0 ? (
                        <Stack direction="row" spacing={0.6} sx={{ flexWrap: 'wrap', gap: 0.6 }}>
                          {version.continuity_lock_ids.map((id) => (
                            <Chip key={id} size="small" variant="outlined" label={id} />
                          ))}
                        </Stack>
                      ) : (
                        <Typography variant="caption" color="text.disabled">
                          这一版没有锁（该镜头没有 in-scope 的锁）
                        </Typography>
                      )}

                      <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 2 }}>
                        负向词
                      </Typography>
                      <Box sx={{ mt: 0.4 }}>
                        <CodeBlock
                          text={version.negative_prompt}
                          maxHeight={120}
                          empty="编译未产生负向词"
                        />
                      </Box>
                    </Grid>

                    <Grid item xs={12}>
                      <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mb: 0.6 }}>
                        参考图槽位（必须声明能决定什么 / 绝不可决定什么）
                      </Typography>
                      <SlotList slots={version.reference_assets} />
                    </Grid>
                  </Grid>
                </>
              )}

              {provLoading && (
                <Stack alignItems="center" sx={{ py: 3 }}>
                  <CircularProgress size={22} />
                </Stack>
              )}
              {prov && !provLoading && (
                <>
                  <Divider sx={{ my: 2 }} />
                  <Stack direction="row" spacing={0.8} alignItems="center" sx={{ mb: 1.4 }}>
                    <AccountTreeOutlinedIcon sx={{ fontSize: 18, color: 'text.secondary' }} />
                    <Typography variant="subtitle2">血缘链</Typography>
                  </Stack>
                  <LineageChain data={prov} direction="prompt" />
                </>
              )}
            </CardContent>
          </Card>
        )}
      </Grid>
    </Grid>
  )
}
