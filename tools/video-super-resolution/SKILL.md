---
name: video-super-resolution
description: 用 Real-ESRGAN 给视频做超分（放大 + 重建细节）的落地要点：为什么选 ncnn-vulkan 单文件版而不是 pip 的 realesrgan、视频该用哪个模型（animevideov3 vs x4plus，实测差 17 倍）、分块流水线如何把 x4 的 82GB 临时帧压到几 GB、音轨/帧数怎么保证不丢、以及「超分完尺寸翻倍但看不出变化」时该怎么查。当需要把视频放大到 2K/4K、AI 生成的视频/图片不够清晰、成片分辨率偏低、或要判断「值不值得超分 / 该花多少时间」时使用。
agent_created: true
---

# 视频超分：Real-ESRGAN 分块流水线

## 什么时候用

- 要把成片/片段**放大**（720p → 1440p / 2K / 4K），且希望是**重建细节**而不是拉糊
- AI 生成的视频分辨率偏低（如图生视频模板硬编码 1376x768），想送回 2K 量级
- 想先判断「超分值不值得」：能出一张 Lanczos vs Real-ESRGAN 的同区域对比图
- 已经跑了超分，但**尺寸翻倍了画面却没见清楚** → 见第七节

## 一、结论先行

| 问题 | 结论 |
|---|---|
| 用什么跑 | `realesrgan-ncnn-vulkan`（单文件 exe + 模型），**不要** pip 的 `realesrgan` 包 |
| 视频用哪个模型 | **`realesr-animevideov3`**（1.2MB，专为视频做的） |
| 默认放大几倍 | **x2**。x4 要多花 2.8 倍时间，换来的差别很细微（实测见第五节） |
| 音轨 | 从原片 `-c copy` 直接过，不走重编码 |
| 临时盘 | 必须分块，否则 x4 跑 5 分钟片子会占 ~82GB |

⚠️ **这个构建版没有 CPU 降级**：`-g -1` 直接报 `invalid gpu device`。必须有能用的 Vulkan GPU。

## 二、装（幂等，跑一次就行）

```bash
python scripts/setup_realesrgan.py                       # 默认装到 ~/.workbuddy/tools/
python scripts/setup_realesrgan.py --proxy http://127.0.0.1:7897   # 走本地代理
```

装完是 `~/.workbuddy/tools/realesrgan-ncnn-vulkan/`：exe + `models/`。
脚本会自己校验模型文件是否齐、并真跑一次 `-h` 确认 exe 能启动（缺 VC++ 运行库会在这里暴露）。

**为什么不用 pip 的 `realesrgan`**：那条路要拖 torch + BasicSR，而 BasicSR 在新 Python
（3.12+）上会因为 `torchvision.transforms.functional_tensor` 被移除而 import 失败，
numpy 2.x 下更糟。ncnn 版不依赖 Python 环境，装完即用。

## 三、跑

```bash
# 默认 x2
python scripts/upscale_video.py -i in.mp4 -o out.mp4

# 先看计划（不执行）
python scripts/upscale_video.py -i in.mp4 -o out.mp4 --dry-run

# x3 出标准 4K（1280x720 → 3840x2160），比 x4 省算力且不用缩放
python scripts/upscale_video.py -i in.mp4 -o out.mp4 -s 3

# x4 超采样后再缩回 1440p（画质略好、耗时 2.8 倍）
python scripts/upscale_video.py -i in.mp4 -o out.mp4 -s 4 --target-width 2560 --target-height 1440

# 出一张画质对比图，先判断值不值得
python scripts/upscale_video.py -i in.mp4 -o cmp.png --compare 12.5 -s 2
```

实测输出（1280x720 / 24fps / 6s 片段，x2）：

```
  输入     1280x720 @ 24fps  0:06  146 帧（逐帧解码）  4.9MB  含音轨(aac)
  超分     realesr-animevideov3  x2  →  2560x1440
  编码     libx264 crf=16 preset=medium
  分块     3 块 × 50 帧   峰值临时盘 ≈ 267.0MB
[2/3] 50 帧  抽帧 0.3s · 超分 7.2s · 编码 1.2s  | 100/146 (68.5%)  已用 0:10  剩 ~0:04
  完成     2560x1440 @ 23.9974fps  0:06  16.7MB   耗时 0:21（0.134 s/帧 实测）
  检查     尺寸 ✓   帧率 ✓   音轨 ✓   时长差 +0.04s   帧数 源 146 / 成片 146 ✓
```

## 四、模型选型（实测，1280x720 输入）

| 模型 | 体积 | 支持倍数 | 单帧耗时 | 说明 |
|---|---|---|---|---|
| **realesr-animevideov3** | 1.2MB | 2 / 3 / 4 | **2.5s**（单帧含启动）· 批量 0.09s(x2) / 0.34s(x4) | 视频专用，SRVGGNetCompact。默认选它 |
| realesrgan-x4plus | 33MB | 仅 4 | **41.7s** ⚠️ | 通用/照片向 RRDBNet，细节更「实」但极重 |
| realesrgan-x4plus-anime | 9MB | 仅 4 | —— | 二次元向 |

⚠️ **x4plus 的 41.7s 是在显存只剩 584MB 时测的**（ComfyUI 占着 15GB）。ncnn 会自动切小
tile 硬扛，代价就是 17 倍慢。要用 x4plus 就先把显存腾出来。**轻量模型在显存紧张时几乎不受影响**，
这也是默认选它的第二个理由。

⚠️ **模型与倍数必须匹配**（`-s 2 -n realesrgan-x4plus` 会在跑起来之后才报错）。脚本已在校验：
x4plus 只支持 4，`realesr-animevideov3` 支持 2/3/4。

**倍数怎么挑（按 1280x720 源算）**：

| 倍数 | 输出 | 用途 |
|---|---|---|
| x2 | 2560x1440 | **默认**。够用且最快 |
| x3 | **3840x2160** | 标准 4K UHD，正好整除、不用缩放。想要 4K 时选这个而不是 x4 |
| x4 | 5120x2880 | 比 4K 还大，通常用不上；想 1440p 又要极致可用 `--target-*` 缩回来 |

⚠️ **上面这张表只对 1280x720 的源成立**。源尺寸一变，x3 就不再是 4K：
**1376x768**（本工程 i2v 输出）x3 = **4128x2304**，不是 3840x2160。
要**精确的 3840x2160**，用 `-s 4 --target-width 3840 --target-height 2160`
（先超采样到 5504x3072 再缩回，质量也比 x3 更好）。
实测该路径：1376x768 / 243 帧 10s → 3840x2160 / 42MB / **2:33**（0.545 s/帧），
帧数 243→243、音轨电平与源完全一致（mean −14.0dB / max −3.5dB）。
编码器对 4K 会自动降 `preset=faster` 以免过慢。

## 五、三个必须心里有数的数

**① 临时帧体积**（1280x720 输入，PNG）

| 阶段 | 尺寸 | 单帧 |
|---|---|---|
| 源帧 | 1280x720 | 0.53MB |
| x2 | 2560x1440 | 2.82MB |
| x4 | 5120x2880 | **11.29MB** |

一段 5 分钟（7229 帧）的片子：x2 约 20GB、**x4 约 82GB**。所以脚本按 chunk 抽帧 →
超分 → 立刻编码成 segment 再删帧，峰值占用 ≈ `2 × chunk × 单帧体积`。
默认 `--chunk 240`（x4 下峰值约 5GB），跑长片且盘不宽裕时调到 60–120。

**② 吞吐**（实测，1280x720 源 / 146 帧 6s 片段，含抽帧与编码）

| 路径 | s/帧 | 输出 | 6s 片段 | 5 分钟成片约 |
|---|---|---|---|---|
| **x2 直出** | **0.13–0.15** | 2560x1440 | 16.4MB | **~16 分钟** |
| x3 直出（4K） | 0.313 | 3840x2160 | 29.9MB | ~38 分钟 |
| x4 超采样→1440p | 0.375 | 2560x1440 | 15.8MB | ~45 分钟 |
| x4 直出 | ~0.34 仅超分 | 5120x2880 | —— | 更久，文件也最大 |

**③ x4 超采样值不值** —— 实测同区域对比：差别**很细微**（光晕边缘略干净），
代价 2.8 倍时间。**默认 x2，要 4K 就上 x3，别默认上 x4。**

## 六、踩过的坑

1. **先出一张对比图再决定跑不跑全片**：`--compare` 左半是 Lanczos 同倍数、右半是
   Real-ESRGAN 同倍数。实测 x2 下 Real-ESRGAN 的文字边缘与光带锐度**明显**优于 Lanczos ——
   这就是「超分重建」与「纯拉大」的区别，也是判断值不值得的依据。

2. ⚠️ **`-c copy` 切出来的片段，`nb_frames` 可能是错的**。实测一个 6.125s@24fps 的片段报
   `nb_frames=258`，实际只有 146 帧（时长×帧率才是对的）。脚本以「时长×帧率」为准，
   两者矛盾时**自动逐帧解码核实一遍**再定案。

3. ⚠️ **续跑必须校验分块完整性**。分块写到一半被杀会留下「能读、但帧数不足」的文件。
   脚本对已有分块做**逐帧解码计数**，对不上就重做；不要改成只看「文件存在」——
   那会在成片里静默缺帧。

4. **拼接后帧数要验**。分块 + concat 最大的风险就是边界缺帧。默认比对元数据，
   要绝对确定加 `--verify-frames`（真解码逐帧数，慢但定案）。
   实测：源 124 → 成片 124、源 146 → 成片 146，**精确一致**。

5. **音轨是 copy 过去的，不要重编码**。实测输出与源的 aac 参数完全一致
   （2ch/44100/时长 6.022993 完全相同），电平也一致（mean −28.6dB / max −14.0dB）。
   收尾用 `-t <片长>` 卡片长，**不要用 `-shortest`**（音轨更短时会把画面砍掉且不报错）。

6. **变帧率（VFR）源会累积漂移**。脚本探测到 `r_frame_rate ≠ avg_frame_rate` 会告警。
   先转成恒定帧率（`-vsync cfr`）再超分。

7. **临时目录删不掉要出声**。Windows 上偶发句柄占用导致 rmtree 部分失败；
   静默 `ignore_errors=True` 会留下半成品树，下次续跑把它当完整分块。

8. **`-m` 显式传模型目录**。默认值是相对路径 `models`（这个构建版按 exe 位置解析，
   但显式传才能让 `REALESRGAN_MODELS` 覆盖生效）。

## 七、验收：怎么确认「真的成了」

超分最迷惑人的失败是**尺寸翻倍了但画面没变清楚**。按顺序查：

```bash
# ① 尺寸对不对
ffprobe -v error -select_streams v:0 -show_entries stream=width,height -of csv=p=0 out.mp4

# ② 帧数有没有丢（分块拼接的风险点）
ffprobe -v error -select_streams v:0 -count_frames -show_entries stream=nb_read_frames -of default=nw=1:nk=1 out.mp4

# ③ 音轨在不在、有没有声（有音轨 ≠ 有声音）
ffmpeg -hide_banner -i out.mp4 -af volumedetect -f null - 2>&1 | grep -E "mean_volume|max_volume"

# ④ 画质到底有没有提升 —— 抽同一帧同区域和 Lanczos 比
python scripts/upscale_video.py -i in.mp4 -o cmp.png --compare 3.0 -s 2
```

判断「值得超分吗」的实操标准：**对比图右侧的文字/边缘/光带明显更锐利**才值得跑全片；
两边差不多就说明源本身信息量已经到顶，超分只是花时间。

## 八、本工程（video-genrate）的用法

本工程的图生视频模板（`minimax_h3_i2v`）输出 **1376x768**，而成片链路是 1280x720。
把它超分 x2 正好是 **2752x1536 —— Qwen-Image 2.1 的原生 16:9 尺寸**，
也就是「回到关键帧该有的清晰度」。命令：

```bash
python upscale_video.py \
  -i backend/storage/videos/proj_xxx/video_xxx.mp4 \
  -o backend/storage/videos/proj_xxx/video_xxx_2x.mp4 \
  -s 2 --chunk 120
```

⚠️ 跑之前先看显存：ComfyUI 在跑大模型时会占掉 15GB 以上。轻量模型仍能跑（自动小 tile），
但**别在这时候用 x4plus**。

成本量级：33 镜 × 7s ≈ 230s 成片 = 约 5500 帧，按 x2 的 0.13 s/帧算**约 12 分钟**就能全部超分完
（这跟「图生视频要跑 3.3 小时」是两回事，别混）。建议**逐镜超分**：5s 镜头（120 帧）纯超分约
**16 秒**、含抽帧+编码+拼接端到端约 **27 秒**，可以在出片链路里边生成边超分，不必攒到成片再动。

实测（2026-10-04，**1376x768 源**，比 1280x720 略大）：10.125s / **243 帧** → x2 出 2752x1536，
端到端 **0:54**（≈0.22 s/帧含抽帧编码、纯超分 0.132 s/帧）；帧数 243→243 精确一致，
音轨 `-c copy` 电平零漂移（源/成片同为 mean −14.0dB / max −3.5dB）。
