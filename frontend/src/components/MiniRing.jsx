import { Box, Typography } from '@mui/material'

/**
 * 环形进度指示器。
 * 默认白色系（用于深色封面之上），可通过 props 切换成浅色底上的主题色。
 */
export default function MiniRing({
  value = 0,
  size = 40,
  stroke = 4,
  color = '#fff',
  trackColor = 'rgba(255,255,255,0.35)',
  textColor = '#fff',
}) {
  const r = (size - stroke) / 2
  const c = 2 * Math.PI * r
  const pct = Math.max(0, Math.min(1, Number(value) || 0))
  return (
    <Box sx={{ position: 'relative', width: size, height: size }}>
      <Box
        component="svg"
        width={size}
        height={size}
        sx={{ transform: 'rotate(-90deg)', display: 'block' }}
      >
        <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke={trackColor} strokeWidth={stroke} />
        <circle
          cx={size / 2}
          cy={size / 2}
          r={r}
          fill="none"
          stroke={color}
          strokeWidth={stroke}
          strokeLinecap="round"
          strokeDasharray={c}
          strokeDashoffset={c * (1 - pct)}
        />
      </Box>
      <Typography
        sx={{
          position: 'absolute',
          inset: 0,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          color: textColor,
          fontSize: size <= 44 ? 11 : 13,
          fontWeight: 700,
        }}
      >
        {Math.round(pct * 100)}
      </Typography>
    </Box>
  )
}
