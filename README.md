# WoWs Render — 战舰世界高清渲染管线

从 **World of Warships（战舰世界）** 游戏客户端里导出舰船 3D 模型，用 Blender + Cycles 离线渲染成高清图片。

- 自动取景：用真实顶点做投影二分搜索，任何船型都能完整装进画面
- 6 个视角（主视角 / 侧舷 / 艉侧 / 艏部 / 俯视 / 低角）× 4 种光照（白昼 / 日落 / 蓝调 / 阴天）
- 分辨率 1080p → **16K**，采样 32 → 256
- 两种入口：**本地网页 UI**（选船、排队、看进度、内联预览）+ **命令行批量**
- 中文舰名直接从游戏自带 `global.mo` 提取，795 艘零漏配

> **本仓库不含任何游戏素材。** 没有图片、没有模型、没有游戏文件、没有第三方工具二进制。
> 仓库里只有脚本和两张已生成的舰船目录 JSON，其余都要在你本机自己跑出来或自己下载。

---

## 目录

- [工作流程](#工作流程)
- [快速开始](#快速开始)
- [网页 UI](#网页-ui)
- [命令行批量](#命令行批量)
- [参数说明](#参数说明)
- [输出结构](#输出结构)
- [中文舰名](#中文舰名)
- [性能参考](#性能参考)
- [文件说明](#文件说明)
- [配置路径](#配置路径不用改代码)
- [已知限制](#已知限制)
- [常见问题](#常见问题)

---

## 工作流程

```
游戏客户端                    _tools/                      _render/
───────────                  ─────────                    ─────────
GameParams + .geometry  ──▶  wows-gltf-exporter  ──▶  Bismarck.raw.glb
                                                          │
                                                          ▼
                                                    sanitize_glb.py
                                                   （剔除空 mesh）
                                                          │
                                                          ▼
                                              Blender 4.5 + Cycles
                                            （自动取景 / 光照 / 海面）
                                                          │
                                                          ▼
                                 WoWs_Renders/<国家>/<舰种>/Tier <级>/<Key>/
                                                  hero.png  side.png …
```

四个阶段：

1. **导出** — `wows-gltf-exporter.exe` 从游戏客户端解析模型和贴图，输出 GLB
2. **清理** — `sanitize_glb.py` 删掉导出器产生的空 mesh，否则 Blender 导入会失败
3. **渲染** — `render_ship.py` 在 Blender 里自动取景、打光、加程序化海面，逐视角出图
4. **收集** — 网页 UI 把结果内联展示；命令行模式直接落盘

---

## 快速开始

### 1. 装依赖

| 依赖 | 版本 | 获取方式 |
|---|---|---|
| Windows | 10 / 11 | 渲染链目前只在 Windows 验证过 |
| Python | 3.11+ | [python.org](https://www.python.org/downloads/) |
| Blender | 4.5.x | [blender.org](https://www.blender.org/download/)，解压到 `_tools/blender-4.5.10-windows-x64/` |
| [wows-model-exporter](https://github.com/wows-tools/wows-model-exporter) | latest release | 解压到 `_tools/model-exporter/` |
| [wows-depack](https://github.com/wows-tools/wows-depack) | latest release | 解压到 `_tools/wows-depack/` |
| World of Warships | 客户端 | 提供模型与 `global.mo` |
| NVIDIA GPU | 建议 | Cycles GPU 渲染；纯 CPU 也能跑，但慢一个数量级 |

目录名必须和上面一致，否则脚本会报 `missing dependency: ...`。

### 2. 配置游戏路径

脚本内置了默认路径，但**你的机器多半不一样**。用环境变量指定一次即可：

```powershell
$env:WOWS_GAME_DIR = "你的游戏安装目录"
```

详见 [配置路径](#配置路径不用改代码)。

### 3. 启动

```
双击  _render\启动渲染服务.bat
```

浏览器自动打开 `http://127.0.0.1:8770`。

---

## 网页 UI

**左栏**

| 控件 | 可选值 |
|---|---|
| 国家 | 全部 / 英联邦 / D系 / Y系 / M系 / R系 / S系 / F系 / I系 … |
| 舰种 | 全部 / 战列舰 / 巡洋舰 / 驱逐舰 / 航母 / 潜艇 |
| 级别 | 全部 / I 级 ~ ★ 级 |
| 战舰 | 795 艘，显示为「中文名 英文名 · 级别 舰种 · 国家」 |
| 分辨率 | 1080p / 2K / 4K / 8K / 16K |
| 视角 | 全部视角 + 6 个单项 |
| 采样 | 32 / 64 / 128 / 256 |
| 光照 | 白昼 / 日落 / 蓝调 / 阴天 |

底部实时估算耗时。

**右栏**

- 实时日志（导出 → 清理 → 渲染）
- 出图后内嵌显示，点图放大，右侧「下载」直接存盘

换端口：`启动渲染服务.bat 8780`。关掉黑窗口即停服务。

> **编码注意**：`启动渲染服务.bat` 是 **GBK 编码**。cmd.exe 按系统代码页解析批处理，存成 UTF-8 会让中文变乱码并报错。改动用记事本保存时选「ANSI」。仓库已用 `.gitattributes` 把它标为 `-text`，防止 Git 改动换行符。

---

## 命令行批量

适合无人值守跑一整批。

```powershell
cd _render

# 单艘
.\render_all.ps1 -Ships PGSB108_Bismarck

# 某个国家某个舰种的全 8 级
.\render_all.ps1 -Nation Germany -Type Battleship -Tier 8

# 多艘指定
.\render_all.ps1 -Ships PGSB108_Bismarck,PJSB007_Kongo_1942

# 全量（很久）
.\render_all.ps1 -Everything -SkipExisting

# 4K、128 采样、双视角、日落光
.\render_all.ps1 -Nation USA -Tier 10 -Res 3840x2160 -Samples 128 -Views hero,side -Preset sunset
```

---

## 参数说明

| 参数 | 默认值 | 说明 |
|---|---|---|
| `-Ships` | — | 船 Key，支持模糊匹配（`-Ships Bismarck` 也行） |
| `-Nation` | 全部 | `Germany` / `USA` / `United_Kingdom` / `Russia` … |
| `-Type` | 全部 | `Battleship` / `Cruiser` / `Destroyer` / `AirCarrier` / `Submarine` |
| `-Tier` | 全部 | `1`–`11` |
| `-Everything` | 关 | 忽略上面的筛选，跑全部 |
| `-Res` | `3840x2160` | 宽x高 |
| `-Samples` | `128` | Cycles 采样数 |
| `-Views` | `hero,side,bow,top` | 逗号分隔；可选 `hero` `side` `stern` `bow` `top` `low` |
| `-Preset` | `day` | `day` / `sunset` / `blue` / `storm` |
| `-TexSize` | `4096` | 贴图分辨率上限 |
| `-SkipExisting` | 关 | 主视角图已存在则跳过整艘船 |
| `-KeepGlb` | 关 | 保留中间 `.glb`（调试用） |
| `-GameDir` | 环境变量/内置值 | 覆盖游戏安装目录 |
| `-Python` | 自动探测 | 覆盖 Python 解释器路径 |

视角和光照的数值定义在 `_render/render_ship.py` 顶部的 `VIEW_PRESETS` 和 `PRESETS`，想加新视角改这两张表即可。

---

## 输出结构

```
WoWs_Renders/<国家>/<舰种>/Tier <级别>/<船Key>/<视角>.png
```

例：

```
WoWs_Renders\Germany\Battleships\Tier VIII\PGSB108_Bismarck\
    hero.png    主视角，四分之三艏向，略高于甲板
    side.png    正侧舷
    stern.png   四分之三艉向
    bow.png     正艏向，低机位
    top.png     高位俯视，看甲板布局
    low.png     贴水面低角
```

国家/舰种目录做了统一映射：`United_Kingdom → UK`、`Russia → USSR`、`AirCarrier → AircraftCarriers`。超级战舰为 `Tier Star`。

`_render/render_log.tsv` 记录每次导出的耗时与成败（该文件不入库）。

---

## 中文舰名

不是手写映射表，而是解析游戏自带的 gettext 目录：

```
<游戏目录>\bin\<build>\res\texts\zh\LC_MESSAGES\global.mo
```

共 55,288 条 msgid。舰名消息键形如 `IDS_PGSB108`，程序把 `PGSB108_Bismarck` 拆出基础 ID 去查表，得到「俾斯麦」。

`_render/shipnames_zh.json` 是已经提取好的 **3172 条** `IDS_* → 中文`，所以正常情况下不需要重新跑提取。

> R 系舰名在国服是鱼类单字（`Kongō` = 鲤、`Amagi` = 鲑），这是游戏内正式译名，不是 bug。

游戏更新新增/改名后，重跑 `python build_catalog.py` 即可刷新 `ships.json`。

---

## 性能参考

测试环境：RTX 4060 Laptop / i7-14700HX

| 分辨率 | 采样 | 视角数 | 耗时 |
|---|---|---|---|
| 1280×720 | 16 | 3 | ~290 秒 |
| 1920×1080 | 32 | 1 | ~110 秒 |
| 3840×2160 | 128 | 4 | ~500–600 秒 |
| 7680×4320 | 32 | 1 | ~546 秒 |
| 15360×8640 | 16 | 1 | ~700 秒 |

- **分辨率上限 16K**（15360×8640 = 1.33 亿像素），再高会显存不足崩溃
- 源贴图最大 4096×4096，8K 以上多出来的分辨力来自几何细节，不是贴图
- 任务**串行**执行，一艘跑完才开始下一艘

---

## 文件说明

| 文件 | 作用 |
|---|---|
| `启动渲染服务.bat` | 一键启动网页 UI（自动探测 Python、检查依赖、开浏览器） |
| `server.py` | 本地网页服务：选船 / 任务队列 / 进度推送 / 图片服务 |
| `build_catalog.py` | 合并「船表 + 中文名 + 标签」生成 `ships.json` |
| `extract_names.py` | 从 `global.mo` 提取中文舰名 → `shipnames_zh.json` |
| `render_ship.py` | Blender 渲染脚本：自动取景、光照预设、程序化天空与海面 |
| `sanitize_glb.py` | 剔除空 mesh，修复导出器输出 |
| `render_all.ps1` | 命令行批量渲染 |
| `ships.json` | 795 艘船的中文目录（已生成，可直接用） |
| `shipnames_zh.json` | 3172 条中文舰名（已生成，可直接用） |

运行时生成的 `glb/`（中间模型）和 `render_log.tsv` 不入库。

---

## 配置路径（不用改代码）

游戏安装目录和 Python 解释器都可以通过**环境变量**或**命令行参数**指定，脚本里内置的默认值只在两者都没提供时才生效。

### 游戏安装目录

优先级：**`-GameDir` 参数 > `WOWS_GAME_DIR` 环境变量 > 内置默认值**

```powershell
# 方式一：设置环境变量（推荐，对全部脚本生效）
$env:WOWS_GAME_DIR = "E:\Games\World of Warships"
# 永久生效：
[Environment]::SetEnvironmentVariable("WOWS_GAME_DIR", "E:\Games\World of Warships", "User")

# 方式二：只在本次命令里覆盖
.\render_all.ps1 -Nation Germany -GameDir "E:\Games\World of Warships"
```

`server.py`、`build_catalog.py`、`extract_names.py` 同样读取 `WOWS_GAME_DIR`；`server.py` 还额外支持 `--game-dir`，`build_catalog.py` / `extract_names.py` 支持 `--game`。

### Python 解释器

`render_all.ps1` 的查找顺序：**`-Python` 参数 > `WOWS_PYTHON` 环境变量 > 常见安装路径探测（3.13 / 3.12 / 3.11）> `PATH` 里的 `python`**

```powershell
$env:WOWS_PYTHON = "C:\Python312\python.exe"
# 或
.\render_all.ps1 -Ships Bismarck -Python "C:\Python312\python.exe"
```

`启动渲染服务.bat` 有自己的探测逻辑（同样按 3.13 → 3.12 → 3.11 → `where python` 的顺序），会显示检测到的 Python 和游戏目录；找不到就改里面的 `PY` 变量。

### 其它路径

`_tools/` 下的所有路径都是从脚本位置相对推算的（`_render/` 的父目录 + `_tools/...`），**只要保持目录结构就无需任何配置**。

### 路径不对时的表现

脚本会在启动时打印实际使用的 `python` 和 `game dir`，例如：

```
python   : C:\Program Files\Python313\python.exe
game dir : D:\SteamLibrary\steamapps\common\World of Warships
```

如果游戏目录不存在，会直接给出明确提示，而不是一堆难以理解的报错：

```
game dir not found: Z:\nope
  请设置环境变量 WOWS_GAME_DIR，或用 -GameDir 指定游戏安装目录。
```

---

## 已知限制

- **只在 Windows 验证过**：路径写法、`.bat` 编码、Blender / 导出器的 `.exe` 都是 Windows 形态
- **模型缺陷来自上游** `wows-model-exporter`：部分船体镜像、螺旋桨缺失或错位、鱼雷缺失、老模型不支持
- **默认涂装**：未做迷彩替换
- **海面是程序化材质**，不是游戏内海面效果
- **渲染串行**，没有并行/分布式
- **16K 是硬上限**，再高 OOM
- 需要**本机装有游戏客户端**，脚本不附带任何游戏资源

---

## 常见问题

**Q：报 `missing dependency: ...`？**
`_tools/` 下三样东西缺了或者目录名不对。核对 `blender-4.5.10-windows-x64/blender.exe`、`model-exporter/wows-gltf-exporter.exe`、`model-exporter/wows-list-ships.exe`。

**Q：导出器说找不到船 / 游戏版本不支持？**
客户端更新后模型格式可能变化，升级 `wows-model-exporter` 到最新 release。

**Q：网页空白，或提示 `ships.json 不存在`？**
运行 `python _render/build_catalog.py` 重新生成。它需要 `_tools/model-exporter/wows-list-ships.exe` 和游戏目录都在。

**Q：中文名全是英文？**
游戏缺中文语言包（`texts/zh/LC_MESSAGES/global.mo`）。装中文语言资源后重跑 `build_catalog.py`。

**Q：Blender 渲染报「未返回 RENDER_OK」？**
看 `render_all.ps1` 输出里的 `Traceback`。常见原因是显存不足——降低 `-Res` 或 `-Samples`。

**Q：16K 崩溃？**
显存不够。16K 需要约 1.33 亿像素的渲染缓冲，笔记本显卡基本别想。

---

## 致谢

- [wows-model-exporter](https://github.com/wows-tools/wows-model-exporter) — 游戏模型 → glTF
- [wows-depack](https://github.com/wows-tools/wows-depack) — 游戏资源包解包
- [Blender](https://www.blender.org/) — 渲染器

## 版权与许可

**本仓库的版权边界**

- 本仓库**不包含**任何 Wargaming 素材：没有游戏文件、没有模型、没有图片
- 本仓库**不包含**从 Wiki 或游戏提取的图库数据
- 只有脚本 + 从游戏文本资源派生的**舰名映射**（`shipnames_zh.json`）与舰船属性目录（`ships.json`）

用这些脚本渲染出的图片是游戏模型的**衍生作品**，模型版权归 Wargaming.net 所有。
本仓库不为此类渲染结果授予任何再分发权利——是否公开、如何使用，请自行评估并遵守当地法律与 Wargaming 的最终用户协议。

脚本本身：MIT。
