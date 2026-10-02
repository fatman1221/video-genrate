import { useEffect, useState } from 'react'
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
  Drawer,
  FormControlLabel,
  IconButton,
  Stack,
  Switch,
  TextField,
  Tooltip,
  Typography,
} from '@mui/material'
import CloseIcon from '@mui/icons-material/Close'
import ReplayIcon from '@mui/icons-material/Replay'
import UndoIcon from '@mui/icons-material/Undo'
import CheckCircleIcon from '@mui/icons-material/CheckCircle'
import BlockIcon from '@mui/icons-material/Block'
import PlayArrowIcon from '@mui/icons-material/PlayArrow'
import GraphicEqIcon from '@mui/icons-material/GraphicEq'
import SubtitlesIcon from '@mui/icons-material/Subtitles'
import DescriptionIcon from '@mui/icons-material/Description'
import { STAGE_ICON } from './PipelineFlow'

const STATE_TEXT = {
  SUCCESS: { label: '已完成', color: 'success' },
  RUNNING: { label: '进行中', color: 'primary' },
  FAILED: { label: '失败', color: 'error' },
  PENDING: { label: '待执行', color: 'default' },
  SKIPPED: { label: '已跳过', color: 'default' },
}

const REVIEW_TEXT = {
  APPROVED: { label: '审核通过', color: 'success' },
  REJECTED: { label: '已驳回', color: 'warning' },
  NONE: { label: '未审核', color: 'default' },
}

function fmtDurationMs(ms) {
  if (!ms) return '—'
  const s = ms / 1000
  if (s < 60) return `${s.toFixed(1)} 秒`
  return `${Math.floor(s / 60)} 分 ${Math.round(s % 60)} 秒`
}

function PreviewTile({ item, onOpen }) {
  const isVideo = item.kind === 'video' || item.kind === 'project_output'
  const isAudio = ['voice', 'music', 'sfx'].includes(item.kind)
  const isImage = ['image', 'character', 'scene'].includes(item.kind)
  const isText = item.kind === 'subtitle'

  return (
    <Box
      onClick={() => (isVideo ? onOpen(item) : null)}
      sx={{
        position: 'relative',
        borderRadius: 2,
        overflow: 'hidden',
        border: '1px solid', borderColor: 'divider',
        bgcolor: 'action.hover',
        aspectRatio: '16 / 10',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        cursor: isVideo ? 'pointer' : 'default',
        '&:hover .tile-ov': { opacity: isVideo ? 1 : 0 },
      }}
    >
      {isImage && (
        <Box component="img" src={item.url} alt={item.name} sx={{ width: '100%', height: '100%', objectFit: 'cover' }} />
      )}
      {isVideo && (
        <>
          {item.poster ? (
            <Box component="img" src={item.poster} alt={item.name} sx={{ width: '100%', height: '100%', objectFit: 'cover' }} />
          ) : (
            <Box component="video" src={item.url} muted sx={{ width: '100%', height: '100%', objectFit: 'cover' }} />
          )}
          <Box
            className="tile-ov"
            sx={{
              position: 'absolute', inset: 0, bgcolor: 'rgba(26,23,38,0.42)', opacity: 0,
              transition: 'opacity .2s ease', display: 'flex', alignItems: 'center', justifyContent: 'center',
            }}
          >
            <PlayArrowIcon sx={{ color: '#fff', fontSize: 30 }} />
          </Box>
        </>
      )}
      {isAudio && <GraphicEqIcon sx={{ fontSize: 26, color: 'primary.light' }} />}
      {isText && (
        <Stack alignItems="center" spacing={0.4} sx={{ px: 1.5 }}>
          <SubtitlesIcon sx={{ fontSize: 20, color: 'primary.light' }} />
          <Typography variant="caption" color="text.secondary" sx={{ textAlign: 'center', lineHeight: 1.35 }}>
            {(item.text || item.name || '').slice(0, 26)}
          </Typography>
        </Stack>
      )}
      <Box sx={{ position: 'absolute', inset: 'auto auto 0 0', right: 0, px: 0.8, py: 0.3, bgcolor: 'rgba(26,23,38,0.55)' }}>
        <Typography sx={{ color: '#fff', fontSize: 10 }}>
          {item.duration ? `${Number(item.duration).toFixed(1)}s` : item.name?.slice(0, 14)}
        </Typography>
      </Box>
    </Box>
  )
}

/**
 * 节点详情与操作面板：观察产出 → 审核 → 回退 / 重新生成。
 */
export default function StagePanel({
  node, open, onClose, busy, onRollback, onRegenerate, onReview,
}) {
  const [comment, setComment] = useState('')
  const [fullReset, setFullReset] = useState(true)
  const [player, setPlayer] = useState(null)
  const [confirm, setConfirm] = useState(null)

  useEffect(() => {
    setComment('')
    setFullReset(true)
    setConfirm(null)
  }, [node?.step_key, open])

  if (!node) {
    return <Drawer anchor="right" open={false} onClose={onClose} />
  }

  const Icon = STAGE_ICON[node.step_key] || DescriptionIcon
  const stateMeta = STATE_TEXT[node.state] || STATE_TEXT.PENDING
  const reviewMeta = REVIEW_TEXT[node.review_status] || REVIEW_TEXT.NONE
  const isDone = node.state === 'SUCCESS'
  const rejected = node.review_status === 'REJECTED'

  const act = async (fn) => {
    await fn()
    setConfirm(null)
  }

  return (
    <Drawer
      anchor="right"
      open={open}
      onClose={onClose}
      PaperProps={{ sx: { width: { xs: '100%', sm: 460 }, p: 0 } }}
    >
      {/* 头部 */}
      <Stack direction="row" alignItems="center" spacing={1.5} sx={{ px: 2.5, pt: 2, pb: 1.5 }}>
        <Box
          sx={{
            width: 40, height: 40, borderRadius: '50%', bgcolor: 'primary.main', color: '#fff',
            display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0,
          }}
        >
          <Icon sx={{ fontSize: 21 }} />
        </Box>
        <Box sx={{ flex: 1, minWidth: 0 }}>
          <Typography variant="h6">{node.label}</Typography>
          <Typography variant="caption" color="text.secondary">
            {node.description}
          </Typography>
        </Box>
        <IconButton size="small" onClick={onClose}>
          <CloseIcon fontSize="small" />
        </IconButton>
      </Stack>

      <Stack direction="row" spacing={0.8} sx={{ px: 2.5, pb: 1.5, flexWrap: 'wrap', gap: 0.8 }}>
        <Chip size="small" label={stateMeta.label} color={stateMeta.color} />
        {node.reviewable && <Chip size="small" variant="outlined" label={reviewMeta.label} color={reviewMeta.color} />}
        <Chip size="small" variant="outlined" label={node.stat} />
        {node.attempts > 1 && <Chip size="small" variant="outlined" label={`第 ${node.attempts} 次`} />}
        {node.rollback_count > 0 && (
          <Chip size="small" variant="outlined" color="warning" label={`回退 ${node.rollback_count} 次`} />
        )}
      </Stack>

      <Divider />

      <Box sx={{ px: 2.5, py: 2, flex: 1, overflowY: 'auto' }}>
        {node.error && (
          <Alert severity="error" sx={{ mb: 2, fontSize: 12.5 }}>
            {node.error}
          </Alert>
        )}
        {rejected && node.review_comment && (
          <Alert severity="warning" sx={{ mb: 2, fontSize: 12.5 }}>
            驳回原因：{node.review_comment}
          </Alert>
        )}

        {/* 产出 */}
        <Typography variant="overline" color="text.secondary">
          产出
        </Typography>
        {node.previews?.length ? (
          <Box
            sx={{
              display: 'grid',
              gridTemplateColumns: 'repeat(2, 1fr)',
              gap: 1,
              mt: 1,
            }}
          >
            {node.previews.map((item) => (
              <PreviewTile key={item.asset_id} item={item} onOpen={setPlayer} />
            ))}
          </Box>
        ) : (
          <Typography variant="body2" color="text.disabled" sx={{ mt: 0.5 }}>
            暂无产出
          </Typography>
        )}

        {/* 元信息 */}
        <Stack direction="row" spacing={3} sx={{ mt: 2.5 }}>
          {[
            ['耗时', fmtDurationMs(node.duration_ms)],
            ['尝试', `${node.attempts} 次`],
            ['更新', node.updated_at ? node.updated_at.slice(5, 16).replace('T', ' ') : '—'],
          ].map(([k, v]) => (
            <Box key={k}>
              <Typography variant="caption" color="text.disabled" display="block">
                {k}
              </Typography>
              <Typography variant="body2" fontWeight={600}>
                {v}
              </Typography>
            </Box>
          ))}
        </Stack>

        {/* 审核 */}
        {node.reviewable && (
          <>
            <Divider sx={{ my: 2.5 }} />
            <Typography variant="overline" color="text.secondary">
              审核
            </Typography>
            {node.reviewed_by && (
              <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 0.6 }}>
                最近由 {node.reviewed_by} 于 {String(node.reviewed_at || '').slice(5, 16).replace('T', ' ')} ·{' '}
                {reviewMeta.label}
              </Typography>
            )}
            <TextField
              size="small"
              fullWidth
              multiline
              minRows={2}
              sx={{ mt: 1.2 }}
              label="审核意见"
              placeholder={rejected ? '说明问题，便于按意见重做' : '可选：备注本次审核的判断依据'}
              value={comment}
              onChange={(e) => setComment(e.target.value)}
            />
            <Stack direction="row" spacing={1} sx={{ mt: 1.2 }}>
              <Button
                fullWidth
                variant="outlined"
                color="success"
                startIcon={<CheckCircleIcon />}
                disabled={busy || !isDone}
                onClick={() => onReview(node, 'APPROVED', comment)}
              >
                审核通过
              </Button>
              <Button
                fullWidth
                variant="outlined"
                color="warning"
                startIcon={<BlockIcon />}
                disabled={busy || !isDone}
                onClick={() => setConfirm('reject')}
              >
                驳回
              </Button>
            </Stack>
            {!isDone && (
              <Typography variant="caption" color="text.disabled" sx={{ mt: 0.8, display: 'block' }}>
                节点完成生产后才能审核
              </Typography>
            )}
          </>
        )}

        {/* 重新生成 */}
        <Divider sx={{ my: 2.5 }} />
        <Typography variant="overline" color="text.secondary">
          重新生成
        </Typography>
        <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 0.6 }}>
          只重跑「{node.label}」这一个节点；下游节点会被自动置为待执行，避免出现半新半旧。
        </Typography>
        <FormControlLabel
          sx={{ mt: 0.5 }}
          control={<Switch size="small" checked={fullReset} onChange={(e) => setFullReset(e.target.checked)} />}
          label={<Typography variant="caption">全量重跑（关闭则只补缺失的部分）</Typography>}
        />
        <Button
          fullWidth
          variant="contained"
          startIcon={<ReplayIcon />}
          disabled={busy}
          sx={{ mt: 1 }}
          onClick={() => onRegenerate(node, { reset: fullReset, reason: comment })}
        >
          重新生成此节点
        </Button>

        {/* 回退 */}
        {isDone && (
          <>
            <Divider sx={{ my: 2.5 }} />
            <Typography variant="overline" color="text.secondary">
              回退
            </Typography>
            <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 0.6 }}>
              回到「{node.label}」这一步重来：该节点与全部下游重置为待执行，已有产物仍保留在素材中心可对比。
            </Typography>
            <Button
              fullWidth
              variant="outlined"
              color="warning"
              startIcon={<UndoIcon />}
              disabled={busy}
              sx={{ mt: 1 }}
              onClick={() => setConfirm('rollback')}
            >
              回退到此节点
            </Button>
          </>
        )}
      </Box>

      {/* 产出播放 */}
      <Dialog open={Boolean(player)} onClose={() => setPlayer(null)} maxWidth="md" fullWidth>
        <DialogTitle sx={{ fontSize: 14 }}>{player?.name}</DialogTitle>
        <DialogContent>
          {player && (
            <Box component="video" src={player.url} controls autoPlay sx={{ width: '100%', borderRadius: 2, bgcolor: '#000' }} />
          )}
        </DialogContent>
      </Dialog>

      {/* 二次确认 */}
      <Dialog open={Boolean(confirm)} onClose={() => setConfirm(null)} maxWidth="xs" fullWidth>
        <DialogTitle sx={{ fontSize: 15.5 }}>
          {confirm === 'rollback' ? '确认回退？' : '确认驳回？'}
        </DialogTitle>
        <DialogContent>
          <Typography variant="body2" color="text.secondary">
            {confirm === 'rollback'
              ? `将回到「${node.label}」这一步：该节点与全部下游重置为待执行，下游产物引用被清空（文件保留）。`
              : `将驳回「${node.label}」，并按意见回退重做，下游产物一并失效。`}
          </Typography>
        </DialogContent>
        <DialogActions sx={{ px: 3, pb: 2 }}>
          <Button onClick={() => setConfirm(null)}>取消</Button>
          <Button
            variant="contained"
            color={confirm === 'rollback' ? 'warning' : 'error'}
            disabled={busy}
            onClick={() =>
              act(() =>
                confirm === 'rollback'
                  ? onRollback(node, comment)
                  : onReview(node, 'REJECTED', comment, { autoRollback: true, regenerate: true }),
              )
            }
          >
            确认执行
          </Button>
        </DialogActions>
      </Dialog>
    </Drawer>
  )
}
