# selftest —— 视频超分网页的自测脚本

先起服务，再逐个跑（Windows / Git Bash）：

```bash
cd ..                       # web/
unset PYTHONPATH
python server.py --port 8090
```

```bash
# 端到端：上传 → 计划 → 执行 → ffprobe 独立核验 → Range → 非法参数
python selftest/e2e_video.py

# 取消 → 无孤儿进程 → 分块保留 → 重跑复用分块 → 删除
python selftest/e2e_cancel.py
```

`e2e_cancel.py` 会真的上传并跑几轮，**两个脚本串行跑**（GPU 只有一块）。
不需要 GPU 的诊断探针：

| 脚本 | 回答什么问题 |
|---|---|
| `probe_frames.py <任务号>` | 「已完成帧数」全程单调不减吗？块内有中间值吗？（口径错时它会明确报「倒退」） |
| `probe_procs.py` | `tasklist` 到底能不能看见 `realesrgan-ncnn-vulkan.exe`？（分清「检测手段失效」和「采样时机不对」） |
| `probe_progress.py` | 进度点够密吗、是不是假进度条？ |

## 两条容易踩的测试设计纪律

1. **验「取消后无孤儿」要取进程峰值，不能在某个进度点单次采样。**
   `progress` 跨过「第 1 块完成 / 第 2 块开始」的那一刻是**分块边界**，此刻 ffmpeg 在跑、
   ncnn 合理地不在跑 —— 单次采样必然 `realesrgan=0`，是假阴性。
2. **不要信 API 自报。** 尺寸/帧数/音轨用 `ffprobe` 独立核一遍，成片按 `Content-Length`
   与磁盘文件大小对齐，否则「接口说 243 帧、文件其实缺帧」这类错会一路漏到成片。

## 依赖

只用标准库。`ffprobe` 走环境里的 ffmpeg 目录（`FFPROBE_BIN`，或 PATH 中的 `ffprobe`）。
