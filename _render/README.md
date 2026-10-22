# 战舰世界 高清渲染 — 网页版

双击 **`启动渲染服务.bat`** 即可，浏览器会自动打开 `http://127.0.0.1:8770`。

## 启动

```
双击  启动渲染服务.bat
```

- 会自动定位 Python、检查依赖、生成船只清单
- 想换端口：命令行运行 `启动渲染服务.bat 8780`
- 关闭黑色命令行窗口 = 停止服务

若提示找不到 Python，用记事本打开 bat，把 `PY` 改成你的 python.exe 完整路径。

> 注意：bat 文件必须是 **GBK 编码**保存，否则中文会变成乱码并报错（cmd.exe 用系统代码页解析批处理）。已经处理好，改动用记事本保存时选「ANSI」。

## 界面

**左栏**

| 控件 | 说明 |
|---|---|
| 国家 | 全部国家 / 英联邦 Commonwealth / D系 Germany / Y系 United_Kingdom … |
| 舰种 | 全部舰种 / 战列舰 Battleship / 巡洋舰 Cruiser / 驱逐舰 Destroyer … |
| 级别 | 全部级别 / I级 (Tier 1) ~ X级 (Tier 10) / ★级 |
| 战舰 | 795 艘，显示为「中文名 英文名 · 级别 舰种 · 国家中英文」。默认**全选**当前筛选出的所有船，按列表顺序批量生成 |
| 分辨率 | 1080p / 2K / 4K / 8K / 16K（上限） |
| 视角 | **全部视角** + 6 个单项：主视角 / 侧舷 / 艉侧 / 艏部 / 俯视 / 低角 |
| 采样 | 32 / 64 / 128 / 256 |
| 光照 | 白昼 / 日落 / 蓝调 / 阴天 |

底部实时估算耗时。

### 批量生成（一次跑完筛选结果）

选好国家 / 舰种 / 级别后，战舰列表会**默认全选**筛选出的所有船（如
France + 战列舰 + 全部级别 = 17 艘），点「开始生成」即按列表顺序一艘接一艘渲染：

- 列表是**多选**列表（`multiple`），每行都能单独勾选；左侧「全选本列表」勾选框一键全选或全清（部分选中时显示为半选状态），标签上实时显示「共 N 艘，已选 M 艘」
- **单击某一行 = 加选/减选那一艘**，不会把其它已选的清掉；Ctrl / Shift 仍是「追加 / 连续选择」
- 提交的是列表里**被勾选的每一艘**（按列表顺序），不是「当前高亮的那一艘」——渲染顺序从第一行开始
- 底部提示「将按顺序生成 N 艘 · 每艘约 X · 合计约 Y」
- 右栏顶部有进度条、完成/跳过/失败计数和剩余时间估算
- 右栏中间是逐艘队列：每艘显示状态（等待中 / 导出模型 / 提取贴图 / 渲染中 / 完成 / 跳过 / 失败）与耗时，鼠标悬停失败的条目可看错误原因
- 「取消任务」：当前这艘渲染完后停止，其余标记为「已取消」
- 刷新页面会自动重新接上正在跑的任务（`GET /api/jobs`）
- 同一时间只允许一个批次（避免多个 Blender 抢显卡、抢 `glb/` 目录）；重复提交会提示「已有任务在跑」

API：`POST /api/render` 传 `{"keys": [...]}` 或 `{"nation","type","tier"}` 即可批量，
单艘仍可用旧的 `{"key": "..."}`；`GET /api/job/<id>` 返回 `total / index / counts / ships[]`。

**右栏**

- 实时日志（导出 → 清理 → 渲染）
- 出图后内嵌显示，点图放大，右侧「下载」直接存盘

## 中文名从哪来

不是手写映射表，而是从游戏自带的翻译文件里提取的：

```
<游戏目录>\bin\<build>\res\texts\zh\LC_MESSAGES\global.mo
```

这是 gettext 二进制目录（55,288 条 msgid），舰名键形如 `IDS_PGSB108`。
渲染页把 `PGSB108_Bismarck` 拆出基础 ID `PGSB108` 去查表，得到「俾斯麦」。

795 艘**全部命中，零漏配**。

注意 R 系舰名在国服里是鱼类单字（Kongō = 鲤、Amagi = 鲑），这是游戏内的正式译名，不是错误。

## 输出位置

扁平结构，只有国家一层目录，其余信息都在文件名里：

```
WoWs_Renders\<国家>\<船Key>_<舰种>_T<级别>_<视角>_<宽>x<高>.png
```

例：
```
WoWs_Renders\Germany\PGSB108_Bismarck_Battleship_T8_hero_1920x1080.png
                     PGSB108_Bismarck_Battleship_T8_side_1920x1080.png
                     PGSB108_Bismarck_Battleship_T8_bow_1920x1080.png
                     PGSB108_Bismarck_Battleship_T8_top_1920x1080.png
```

放在文件名里的原因：一层层点进 `舰种/Tier/舰船/` 太深，平铺后一个国家目录下
即可直接浏览全部该国渲染图，并可按文件名排序筛选。超级战舰级别记为 `TStar`。

## 实测耗时（RTX 4060 Laptop / i7-14700HX）

| 分辨率 | 采样 | 视角 | 耗时 |
|---|---|---|---|
| 1280×720 | 16 | 3 | ~290 秒 |
| 1920×1080 | 32 | 1 | ~110 秒 |
| 3840×2160 | 128 | 4 | ~500–600 秒 |
| 7680×4320 | 32 | 1 | ~546 秒 |
| 15360×8640 | 16 | 1 | ~700 秒 |

**分辨率上限 16K（15360×8640 = 1.33 亿像素）**，再高会显存不足崩溃。
源贴图最大 4096×4096，8K 以上多出的分辨率来自几何而非贴图。

## 命令行批量

```powershell
cd D:\xupeiyu\obsidian-xupeiyu\1.tools\warships\_render

.\render_all.ps1 -Ships PGSB108_Bismarck
.\render_all.ps1 -Nation Germany -Type Battleship -Tier 8
.\render_all.ps1 -Everything -SkipExisting
```

## 文件

| 文件 | 作用 |
|---|---|
| `启动渲染服务.bat` | 一键启动（自动开浏览器） |
| `server.py` | 网页服务：选船 / 任务队列 / 进度 / 图片服务 |
| `build_catalog.py` | 生成 `ships.json`（合并船表 + 中文名 + 标签） |
| `extract_names.py` | 从 `global.mo` 提取中文舰名 |
| `ships.json` | 795 艘船的中文目录（已生成） |
| `render_ship.py` | Blender 渲染脚本（游戏 PBS 材质/光照/海面/雾） |
| `extract_ship_textures.py` | 从游戏包导出 a/mg/n/ao 贴图为 PNG |
| `sanitize_glb.py` | 修复导出器的空 mesh 问题 |
| `render_all.ps1` | 命令行批量渲染 |

## 游戏 PBS 贴图（重要）

导出器只写颜色贴图，因此默认渲染是"平面灰"的。本流程会额外从游戏包里取出
游戏着色器所用的四张贴图，按游戏的通道语义接进材质：

| 贴图 | 内容 |
|---|---|
| `_a` | 漫反射（颜色） |
| `_mg` | **R = 金属度，B = 光泽度**（实际测出的打包方式） |
| `_n` | 法线，**只用 R/G**，z 由 `sqrt(1-x²-y²)` 重建 |
| `_ao` | 环境光遮蔽 |

贴图默认自动提取到 `textures/`（`render_all.ps1` 与网页服务都会做）。
想退回旧的平面着色：批量脚本加 `-NoPbsMaps`，单次渲染不加 `--textures`。

## 光照与色调映射

游戏用**单方向光 + PMREM 环境光**，没有补光灯，色调映射是一条 GT 曲线。
渲染脚本默认按这个方向走：

| 参数 | 作用 |
|---|---|
| （默认） | 不加补光灯，靠环境光（`--env-boost`，默认 1.45）撑暗部 |
| `--fill-light` | 恢复旧的补光灯（更亮，但不符合游戏） |
| `--gt-tonemap` | GT 曲线近似，**默认开启**；`--no-gt-tonemap` 关闭 |
| `--auto-exposure` | 逐视角测光并修正曝光，**默认开启** |
| `--detail` | 程序化细节法线强度（代替游戏里不存在的 detailMap） |
| `--wetness` / `--specular-mult` | 游戏 `overallWetness` / `specularMult` 全局调制 |
| `--camo 01` | 应用迷彩遮罩（配合 `--camo-colors` 色板） |

关于 GT 曲线的两个实测结论见 [`../docs/game-rendering-pipeline.md`](../docs/game-rendering-pipeline.md) 第 5 节。

## 输出文件名

每张图带分辨率：`<视角>_<宽>x<高>.png`，例如 `hero_3840x2160.png`。
同一艘船不同分辨率的渲染可以并存。

## 迷彩

```powershell
# 提取贴图时一并取回迷彩遮罩
python extract_ship_textures.py --ship PGSB108_Bismarck --nation Germany --type Battleship --out textures --camo

# 渲染时应用（01 / 02 是迷彩编号）
blender -b --factory-startup -P render_ship.py -- --glb ... --out ... --textures textures --camo 01
```

**注意**：迷彩每个区域的实际颜色存在游戏的 `GameParams.data` 里，导出器没有暴露，
默认用近似军灰色板。想精确还原需要另外解析该文件。

单独的提取命令：

```powershell
python extract_ship_textures.py --ship PGSB108_Bismarck --nation Germany --type Battleship --out textures
```

调查细节（通道语义的推导过程、游戏参数表）见 [`../docs/game-rendering-pipeline.md`](../docs/game-rendering-pipeline.md)。

## 已知限制

- 模型缺陷来自 `wows-model-exporter`：船体镜像、螺旋桨缺失/错位、鱼雷缺失、老模型不支持
- 迷彩遮罩可解码，但真实色板在 `GameParams.data` 里，目前用近似军灰色
- 水面为程序化材质
- 同一时间只跑一个批次，批次内串行执行（一艘跑完再下一艘）；失败的那艘会被记录下来，不会中断后续的船
