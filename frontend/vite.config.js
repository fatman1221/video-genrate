import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

/**
 * 说明：本机 Node 运行时带有批量删除保护（单轮 >50 个文件会被拦截）。
 * Vite 在「发现新依赖 → 重新预构建」时会 rm 掉旧的 deps 目录（上百个文件），
 * 从而被拦截并直接终止 dev server。
 * 对策：把用到的依赖全部写进 optimizeDeps.include（一次性预构建，之后不再重优化），
 *      并在启动前清掉 node_modules/.vite 缓存。scripts/dev.sh 已处理。
 *
 * ⚠️ 新增图标时**必须同步登记到下面的 MUI_ICONS**，否则首次引用该图标会触发
 *    Vite 重新预构建，进而撞上批量删除保护、把 dev server 直接杀掉。
 */
const MUI_ICONS = [
  'Add', 'Approval', 'ApprovalOutlined', 'ArrowBack', 'ArticleOutlined', 'AttachFile',
  'AudiotrackOutlined', 'AutoAwesome', 'AutoAwesomeMotion', 'Block', 'Brightness4', 'Brightness7',
  'Check', 'CheckCircle', 'Close', 'CloudOutlined', 'CollectionsOutlined', 'Compare', 'ContentCopy',
  'ContentCut', 'DeleteOutline', 'Description', 'Download', 'EditOutlined', 'Error', 'ErrorOutline',
  'ExpandLess', 'ExpandMore', 'FaceOutlined', 'FaceRetouchingNatural', 'FactCheck', 'GraphicEq',
  'Image', 'InfoOutlined', 'LandscapeOutlined', 'MemoryOutlined', 'MenuBookOutlined', 'Mic',
  'MicNone', 'Movie', 'MovieCreation', 'MovieFilter', 'MovieFilterOutlined', 'MusicNote',
  'OpenInNew', 'PersonOutline', 'PlayArrow', 'PlayCircleOutline', 'PriorityHigh',
  'RecordVoiceOver', 'Refresh', 'Replay', 'RestartAlt', 'RocketLaunch', 'SaveOutlined',
  'SmartToy', 'SubscriptionsOutlined', 'Subtitles', 'TerminalOutlined', 'TuneOutlined', 'Undo',
  'UploadFile', 'Videocam', 'Verified', 'ViewQuilt', 'VolumeUp',
].map((n) => `@mui/icons-material/${n}`)

export default defineConfig({
  plugins: [react()],
  optimizeDeps: {
    include: [
      'react', 'react-dom', 'react-dom/client', 'react-router-dom', 'axios',
      '@mui/material', '@mui/material/styles', '@emotion/react', '@emotion/styled',
      ...MUI_ICONS,
    ],
  },
  server: {
    host: '127.0.0.1',
    port: 5180,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8077', changeOrigin: true },
      '/media': { target: 'http://127.0.0.1:8077', changeOrigin: true },
    },
  },
})
