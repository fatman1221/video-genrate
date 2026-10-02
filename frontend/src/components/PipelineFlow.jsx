import { Box, Stack, Tooltip, Typography } from '@mui/material'
import { alpha, useTheme } from '@mui/material/styles'
import DescriptionOutlinedIcon from '@mui/icons-material/DescriptionOutlined'
import ViewQuiltOutlinedIcon from '@mui/icons-material/ViewQuiltOutlined'
import FaceRetouchingNaturalOutlinedIcon from '@mui/icons-material/FaceRetouchingNaturalOutlined'
import ImageOutlinedIcon from '@mui/icons-material/ImageOutlined'
import MovieOutlinedIcon from '@mui/icons-material/MovieOutlined'
import MicNoneOutlinedIcon from '@mui/icons-material/MicNoneOutlined'
import MusicNoteOutlinedIcon from '@mui/icons-material/MusicNoteOutlined'
import SubtitlesOutlinedIcon from '@mui/icons-material/SubtitlesOutlined'
import AutoAwesomeOutlinedIcon from '@mui/icons-material/AutoAwesomeOutlined'
import ContentCutOutlinedIcon from '@mui/icons-material/ContentCutOutlined'
import MovieFilterOutlinedIcon from '@mui/icons-material/MovieFilterOutlined'
import VerifiedOutlinedIcon from '@mui/icons-material/VerifiedOutlined'
import CheckIcon from '@mui/icons-material/Check'
import PriorityHighIcon from '@mui/icons-material/PriorityHigh'

const STAGE_ICON = {
  script: DescriptionOutlinedIcon,
  storyboard: ViewQuiltOutlinedIcon,
  character: FaceRetouchingNaturalOutlinedIcon,
  image: ImageOutlinedIcon,
  video: MovieOutlinedIcon,
  voice: MicNoneOutlinedIcon,
  music: MusicNoteOutlinedIcon,
  subtitle: SubtitlesOutlinedIcon,
  enhancement: AutoAwesomeOutlinedIcon,
  editing: ContentCutOutlinedIcon,
  composing: MovieFilterOutlinedIcon,
  quality_check: VerifiedOutlinedIcon,
}

/** 节点状态配色。深浅两套：暗底上把主色/底色提亮一档，避免糊成一片。 */
const stateStyle = (dark) => ({
  SUCCESS: {
    fg: dark ? '#A78DFF' : '#6D4AFF',
    bg: dark ? alpha('#9B82FF', 0.18) : '#F1EDFF',
    border: alpha(dark ? '#9B82FF' : '#6D4AFF', dark ? 0.4 : 0.24),
  },
  RUNNING: {
    fg: '#ffffff', bg: dark ? '#7C5CFF' : '#6D4AFF',
    border: dark ? '#7C5CFF' : '#6D4AFF', pulse: true,
  },
  FAILED: {
    fg: dark ? '#F0676B' : '#E2484C',
    bg: dark ? alpha('#F0676B', 0.16) : '#FDEEEF',
    border: alpha(dark ? '#F0676B' : '#E2484C', 0.34),
  },
  PENDING: {
    fg: dark ? '#8B88A0' : '#A9A5B8',
    bg: dark ? alpha('#EDEBF7', 0.06) : '#F5F5F9',
    border: dark ? alpha('#EDEBF7', 0.12) : 'rgba(140,140,155,0.24)',
  },
  SKIPPED: {
    fg: dark ? '#8B88A0' : '#A9A5B8',
    bg: dark ? alpha('#EDEBF7', 0.06) : '#F5F5F9',
    border: dark ? alpha('#EDEBF7', 0.12) : 'rgba(140,140,155,0.24)',
  },
})

const SIZE = 46
const STROKE = 3

function Ring({ value = 0, color, size = SIZE, stroke = STROKE }) {
  const r = (size - stroke) / 2
  const c = 2 * Math.PI * r
  return (
    <Box component="svg" width={size} height={size} sx={{ transform: 'rotate(-90deg)', display: 'block' }}>
      <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="rgba(140,140,155,0.28)" strokeWidth={stroke} />
      <circle
        cx={size / 2}
        cy={size / 2}
        r={r}
        fill="none"
        stroke={color}
        strokeWidth={stroke}
        strokeLinecap="round"
        strokeDasharray={c}
        strokeDashoffset={c * (1 - Math.min(Math.max(value, 0), 1))}
        style={{ transition: 'stroke-dashoffset .5s ease' }}
      />
    </Box>
  )
}

function ReviewBadge({ status }) {
  if (status === 'APPROVED') {
    return (
      <Box
        sx={{
          position: 'absolute', top: -2, right: -2, width: 18, height: 18, borderRadius: '50%',
          bgcolor: 'success.main', color: '#fff', display: 'flex', alignItems: 'center',
          justifyContent: 'center', border: '2px solid', borderColor: 'background.paper',
        }}
      >
        <CheckIcon sx={{ fontSize: 11 }} />
      </Box>
    )
  }
  if (status === 'REJECTED') {
    return (
      <Box
        sx={{
          position: 'absolute', top: -2, right: -2, width: 18, height: 18, borderRadius: '50%',
          bgcolor: 'warning.main', color: '#fff', display: 'flex', alignItems: 'center',
          justifyContent: 'center', border: '2px solid', borderColor: 'background.paper',
        }}
      >
        <PriorityHighIcon sx={{ fontSize: 12 }} />
      </Box>
    )
  }
  return null
}

function Node({ node, selected, onClick }) {
  const theme = useTheme()
  const styleMap = stateStyle(theme.palette.mode === 'dark')
  const Icon = STAGE_ICON[node.step_key] || AutoAwesomeOutlinedIcon
  const style = styleMap[node.state] || styleMap.PENDING
  const isRunning = node.state === 'RUNNING'

  return (
    <Tooltip
      arrow
      placement="top"
      title={
        <Box sx={{ py: 0.3 }}>
          <Typography variant="caption" sx={{ fontWeight: 700, display: 'block' }}>
            {node.label}
          </Typography>
          <Typography variant="caption" sx={{ opacity: 0.8 }}>
            {node.description}
          </Typography>
          {node.error && (
            <Typography variant="caption" sx={{ display: 'block', color: '#FFB4B6', mt: 0.4 }}>
              {String(node.error).slice(0, 80)}
            </Typography>
          )}
        </Box>
      }
    >
      <Box
        onClick={() => onClick?.(node)}
        sx={{
          width: 74,
          cursor: 'pointer',
          textAlign: 'center',
          userSelect: 'none',
          transition: 'transform .18s ease',
          '&:hover': { transform: 'translateY(-3px)' },
          '&:hover .stage-circle': { boxShadow: '0 10px 22px -12px rgba(74,47,204,0.55)' },
        }}
      >
        <Box sx={{ position: 'relative', width: SIZE, height: SIZE, mx: 'auto' }}>
          <Ring value={isRunning ? Math.max(node.ratio, 0.08) : node.ratio} color={style.fg} />
          <Box
            className="stage-circle"
            sx={{
              position: 'absolute', inset: 5, borderRadius: '50%', bgcolor: style.bg,
              border: `1px solid ${style.border}`,
              display: 'flex', alignItems: 'center', justifyContent: 'center',
              outline: selected ? '2px solid #6D4AFF' : 'none',
              outlineOffset: 3,
              animation: style.pulse ? 'stagePulse 1.8s ease-in-out infinite' : 'none',
              '@keyframes stagePulse': {
                '0%,100%': { transform: 'scale(1)' },
                '50%': { transform: 'scale(1.06)' },
              },
            }}
          >
            <Icon sx={{ fontSize: 19, color: style.fg }} />
          </Box>
          <ReviewBadge status={node.review_status} />
        </Box>

        <Typography
          variant="caption"
          sx={{
            display: 'block', mt: 0.8, fontWeight: selected ? 700 : 600, fontSize: 11.5,
            color: selected ? 'primary.main' : 'text.primary',
            whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
          }}
        >
          {node.label}
        </Typography>
        <Typography
          variant="caption"
          sx={{
            color: 'text.disabled', fontSize: 10, display: 'block',
            whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
          }}
        >
          {node.stat}
        </Typography>
      </Box>
    </Tooltip>
  )
}

function Connector({ done }) {
  const dark = useTheme().palette.mode === 'dark'
  return (
    <Box
      sx={{
        flex: '1 1 10px',
        minWidth: 8,
        maxWidth: 26,
        height: 2,
        mt: `${SIZE / 2 - 11}px`,
        borderRadius: 2,
        background: done
          ? 'linear-gradient(90deg, rgba(109,74,255,0.5), rgba(109,74,255,0.28))'
          : `repeating-linear-gradient(90deg, ${dark ? 'rgba(237,235,247,0.18)' : 'rgba(26,23,38,0.14)'} 0 5px, transparent 5px 11px)`,
      }}
    />
  )
}

/**
 * 节点式生产进度：把流水线的每个阶段画成一个节点，
 * 支持点击查看产出 / 回退 / 重新生成 / 审核。
 */
export default function PipelineFlow({ nodes = [], selectedKey, onSelect }) {
  const dark = useTheme().palette.mode === 'dark'
  if (!nodes.length) return null

  return (
    <Box
      sx={{
        overflowX: 'auto',
        overflowY: 'hidden',
        pb: 0.5,
        scrollbarWidth: 'thin',
        '&::-webkit-scrollbar': { height: 6 },
        '&::-webkit-scrollbar-thumb': {
          background: dark ? 'rgba(237,235,247,0.20)' : 'rgba(26,23,38,0.14)',
          borderRadius: 6,
        },
      }}
    >
      <Stack
        direction="row"
        alignItems="flex-start"
        sx={{ minWidth: nodes.length * 74 + 60, px: 0.5 }}
      >
        {nodes.map((node, idx) => (
          <Stack key={node.step_key} direction="row" alignItems="flex-start">
            {idx > 0 && <Connector done={nodes[idx - 1].state === 'SUCCESS'} />}
            <Node node={node} selected={selectedKey === node.step_key} onClick={onSelect} />
          </Stack>
        ))}
      </Stack>
    </Box>
  )
}

export { STAGE_ICON }
