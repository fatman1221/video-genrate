import { Box, Chip, Stack, Typography } from '@mui/material'
import CheckCircleIcon from '@mui/icons-material/CheckCircle'
import ErrorOutlineIcon from '@mui/icons-material/ErrorOutline'

/**
 * 血缘链可视化。
 *
 * 后端两个方向返回的结构不同，这里统一成"自下而上"的阅读顺序：
 *
 * - `direction="asset"`  → `GET /api/assets/{id}/provenance`
 *   `{ chain:{asset,prompt_version,prompt,shot,scene,storyboard,project,task,compiled_from,references},
 *      missing:[], complete:bool, summary }`
 * - `direction="prompt"` → `GET /api/prompts/{id}/provenance`
 *   `{ chain:{prompt,versions,compiled_from,produced_assets,shot,scene}, summary }`
 *
 * 设计立场：反查的意义是回答「当时到底依据什么、发了什么指令」，
 * 所以节点要把**名字和版本号**摊开，只给一个 id 等于没闭环。
 */

const DASH = '—'
const show = (v) => (v === null || v === undefined || v === '' ? DASH : String(v))

const KIND_LABEL = {
  character: '角色',
  location: '地点',
  prop: '道具',
  style: '形态卡',
  asset: '素材',
}

function Step({ label, title, meta = [], missing, last, warn }) {
  const dot = missing ? 'warning.main' : warn ? 'warning.main' : 'primary.main'
  return (
    <Stack direction="row" spacing={1.2} alignItems="stretch">
      <Stack alignItems="center" sx={{ width: 12, flexShrink: 0 }}>
        <Box sx={{ width: 9, height: 9, borderRadius: '50%', bgcolor: dot, mt: 1.1 }} />
        {!last && <Box sx={{ width: '1px', flex: 1, bgcolor: 'divider', my: 0.3, minHeight: 12 }} />}
      </Stack>
      <Box sx={{ pb: last ? 0 : 1.6, flex: 1, minWidth: 0 }}>
        <Stack direction="row" spacing={0.8} alignItems="center" sx={{ flexWrap: 'wrap', gap: 0.6 }}>
          <Chip size="small" variant="outlined" label={label} />
          <Typography variant="body2" sx={{ fontWeight: 600 }} noWrap title={title}>
            {title}
          </Typography>
          {missing && <Chip size="small" color="warning" label="链路缺失" />}
          {warn && !missing && <Chip size="small" color="warning" variant="outlined" label="已变更" />}
        </Stack>
        {meta.filter(Boolean).length > 0 && (
          <Stack direction="row" spacing={1.2} sx={{ mt: 0.4, flexWrap: 'wrap', gap: 0.4 }}>
            {meta.filter(Boolean).map((m, i) => (
              <Typography key={i} variant="caption" color="text.secondary">
                {m}
              </Typography>
            ))}
          </Stack>
        )}
      </Box>
    </Stack>
  )
}

/** 把 compiled_from 的「快照 + 现存状态」合成一张输入清单。 */
function buildInputs(compiledFrom) {
  if (!compiledFrom) return []
  const snap = compiledFrom.snapshot || {}
  const byKey = {}
  for (const r of compiledFrom.resolved || []) byKey[`${r.kind}:${r.id}`] = r
  const rows = []
  // ⚠️ 编译器会把 style 同时写进 `style` 与 `subjects`（kind="style"），
  //    不去重就会在输入清单里出现两行"形态卡"。
  const covered = new Set()

  if (snap.bible) {
    const res = compiledFrom.bible
    rows.push({
      label: '视觉设定',
      name: res?.title || snap.bible.id,
      version: res?.version ?? snap.bible.version,
      exists: res?.exists !== false,
      meta: ['era/global_rules 参与编译'],
    })
  }
  if (snap.style) {
    const res = compiledFrom.style
    covered.add(`style:${snap.style.id}`)
    rows.push({
      label: '形态卡',
      name: res?.name || snap.style.id,
      version: res?.version ?? snap.style.version,
      exists: res?.exists !== false,
      meta: [res?.form_card ? `form_card ${res.form_card}` : ''],
    })
  }
  for (const s of snap.subjects || []) {
    if (covered.has(`${s.kind}:${s.id}`)) continue
    covered.add(`${s.kind}:${s.id}`)
    const res = byKey[`${s.kind}:${s.id}`]
    const variant = res?.variant
    rows.push({
      label: KIND_LABEL[s.kind] || s.kind,
      name: res?.name || s.id,
      version: res?.version ?? s.version,
      exists: res?.exists !== false,
      meta: [
        variant ? `变体 ${variant.name || variant.code}${variant.exists === false ? '（已删除）' : ''}` : '',
        s.variant?.code ? `快照 ${s.variant.code}` : '',
      ],
    })
  }
  const lockById = {}
  for (const l of compiledFrom.locks || []) lockById[l.id] = l
  for (const l of snap.locks || []) {
    const res = lockById[l.id]
    rows.push({
      label: '连续性锁',
      name: l.code || l.id,
      version: res?.version ?? l.version,
      exists: res?.exists !== false,
      warn: Boolean(res?.surface_changed),
      meta: [l.surface ? `锁面「${l.surface}」` : '', res?.surface_changed ? '锁面已变更' : ''],
    })
  }
  return rows.filter((r) => r.exists !== false || true)
}

function InputList({ compiledFrom }) {
  const rows = buildInputs(compiledFrom)
  if (!rows.length) return null
  const segments = compiledFrom?.snapshot?.segments || []
  return (
    <Box sx={{ mt: 1.6 }}>
      <Stack direction="row" spacing={0.8} alignItems="center" sx={{ mb: 0.8 }}>
        <Typography variant="subtitle2">编译输入快照</Typography>
        <Typography variant="caption" color="text.disabled">
          记的是 id + 版本号（不是哈希 —— 手填哈希必然腐烂）
        </Typography>
      </Stack>
      <Stack spacing={0.6}>
        {rows.map((r, i) => (
          <Stack
            key={i}
            direction="row"
            spacing={1}
            alignItems="baseline"
            sx={{
              px: 1, py: 0.6, borderRadius: 1,
              bgcolor: r.exists === false ? 'warning.light' : 'action.hover',
            }}
          >
            <Chip size="small" label={r.label} variant="outlined" sx={{ minWidth: 72 }} />
            <Typography variant="caption" sx={{ fontWeight: 600, minWidth: 90 }} noWrap>
              {show(r.name)}
            </Typography>
            {r.version !== null && r.version !== undefined && (
              <Typography variant="caption" color="text.secondary">
                v{r.version}
              </Typography>
            )}
            {r.exists === false ? (
              <Stack direction="row" spacing={0.4} alignItems="center">
                <ErrorOutlineIcon sx={{ fontSize: 14, color: 'warning.main' }} />
                <Typography variant="caption" color="warning.main">
                  已被删除
                </Typography>
              </Stack>
            ) : (
              r.warn && (
                <Typography variant="caption" color="warning.main">
                  锁面已变更
                </Typography>
              )
            )}
            <Typography variant="caption" color="text.disabled" sx={{ ml: 'auto' }} noWrap>
              {r.meta.filter(Boolean).join(' · ')}
            </Typography>
          </Stack>
        ))}
      </Stack>
      {segments.length > 0 && (
        <Typography variant="caption" color="text.disabled" sx={{ display: 'block', mt: 0.6 }}>
          八段式实际渲染了 {segments.length} 段：{segments.join(' → ')}
        </Typography>
      )}
    </Box>
  )
}

function ReferenceList({ references }) {
  if (!references?.length) {
    return (
      <Typography variant="caption" color="text.disabled" sx={{ display: 'block', mt: 1 }}>
        这一版没有使用参考图（纯文本编译）
      </Typography>
    )
  }
  return (
    <Stack spacing={0.6} sx={{ mt: 1 }}>
      {references.map((r, i) => (
        <Stack key={i} direction="row" spacing={1} alignItems="baseline" sx={{ px: 1, py: 0.6, borderRadius: 1, bgcolor: 'action.hover' }}>
          <Chip size="small" label={r.kind || 'REF'} variant="outlined" />
          <Typography variant="caption" sx={{ fontWeight: 600 }} noWrap>
            {show(r.role || r.slot || r.id)}
          </Typography>
          <Typography variant="caption" color="text.secondary">
            准入 {show(r.admission_status)}
          </Typography>
          {Array.isArray(r.may_control) && r.may_control.length > 0 && (
            <Typography variant="caption" color="text.disabled" noWrap>
              可决定 {r.may_control.join('/')}
            </Typography>
          )}
          {Array.isArray(r.must_not_control) && r.must_not_control.length > 0 && (
            <Typography variant="caption" color="text.disabled" noWrap>
              不可决定 {r.must_not_control.join('/')}
            </Typography>
          )}
        </Stack>
      ))}
    </Stack>
  )
}

export default function LineageChain({ data, direction = 'asset', onOpenAsset }) {
  if (!data?.chain) {
    return (
      <Typography variant="body2" color="text.secondary" sx={{ py: 3, textAlign: 'center' }}>
        暂无可反查的血缘（该产物不是由编译后的提示词产出的）
      </Typography>
    )
  }
  const { chain } = data
  const missing = data.missing || []
  const compiledFrom = chain.compiled_from

  const steps = direction === 'asset'
    ? [
        { key: 'project', label: '项目', title: show(chain.project?.name),
          meta: [`状态 ${show(chain.project?.workflow_state)}`] },
        { key: 'storyboard', label: '分镜表', title: show(chain.storyboard?.title),
          meta: [chain.storyboard?.visual_style ? `画风 ${chain.storyboard.visual_style}` : ''] },
        { key: 'scene', label: '场景', title: `${show(chain.scene?.code)}${chain.scene?.title ? ` · ${chain.scene.title}` : ''}`,
          meta: [chain.scene?.location ? `地点 ${chain.scene.location}` : ''] },
        { key: 'shot', label: '镜头', title: show(chain.shot?.code),
          meta: [chain.shot?.duration ? `${chain.shot.duration}s` : '', chain.shot?.description || ''] },
        { key: 'prompt', label: '提示词', title: `${show(chain.prompt?.code)}${chain.prompt?.name ? ` · ${chain.prompt.name}` : ''}`,
          meta: [chain.prompt?.type ? `类型 ${chain.prompt.type}` : '', chain.prompt?.latest_version ? `最新 v${chain.prompt.latest_version}` : ''] },
        { key: 'prompt_version', label: '提示词版本', title: `v${show(chain.prompt_version?.version)}`,
          meta: [
            chain.prompt_version?.provider ? `provider ${chain.prompt_version.provider}` : '',
            chain.prompt_version?.resolution || '',
            chain.prompt_version?.width ? `${chain.prompt_version.width}x${chain.prompt_version.height}` : '',
            chain.prompt_version?.recipe?.renderer
              ? `${chain.prompt_version.recipe.renderer}@${chain.prompt_version.recipe.version}` : '',
          ] },
        { key: 'task', label: '生成任务', title: show(chain.task?.type),
          meta: [chain.task?.status ? `状态 ${chain.task.status}` : '',
                 chain.task?.attempts ? `第 ${chain.task.attempts} 次尝试` : '',
                 chain.task?.provider ? chain.task.provider : ''] },
        { key: 'asset', label: '产物', title: show(chain.asset?.name),
          meta: [chain.asset?.type || '', chain.asset?.width ? `${chain.asset.width}x${chain.asset.height}` : '',
                 chain.asset?.status || '', chain.asset?.role || ''] },
      ]
    : [
        { key: 'scene', label: '场景', title: show(chain.scene?.code),
          meta: [] },
        { key: 'shot', label: '镜头', title: show(chain.shot?.code), meta: [] },
        { key: 'prompt', label: '提示词', title: `${show(chain.prompt?.code)}`,
          meta: [chain.prompt?.type ? `类型 ${chain.prompt.type}` : '',
                 chain.prompt?.latest_version ? `最新 v${chain.prompt.latest_version}` : ''] },
        { key: 'versions', label: '版本历史', title: `${(chain.versions || []).length} 个版本（只增不改）`,
          meta: (chain.versions || []).map((v) => `v${v.version} ${show(v.status)}`) },
        { key: 'produced_assets', label: '已产出素材', title: `${(chain.produced_assets || []).length} 个`,
          meta: (chain.produced_assets || []).map((a) => a.name) },
      ]

  return (
    <Box>
      <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 1.4, flexWrap: 'wrap', gap: 0.8 }}>
        {direction === 'asset' && (
          <Chip
            size="small"
            color={data.complete ? 'success' : 'warning'}
            icon={data.complete ? <CheckCircleIcon /> : <ErrorOutlineIcon />}
            label={data.complete ? '链路完整' : `链路不完整（缺 ${missing.join('、')}）`}
          />
        )}
        <Typography variant="caption" color="text.secondary">
          {data.summary}
        </Typography>
      </Stack>

      {steps.map((s, i) => (
        <Step
          key={s.key}
          label={s.label}
          title={s.title}
          meta={s.meta}
          missing={missing.includes(s.key)}
          last={i === steps.length - 1}
        />
      ))}

      <InputList compiledFrom={compiledFrom} />

      <Box sx={{ mt: 1.6 }}>
        <Typography variant="subtitle2" sx={{ mb: 0.4 }}>
          参考图槽位
        </Typography>
        <ReferenceList references={chain.references || chain.prompt_version?.reference_assets} />
      </Box>
    </Box>
  )
}
