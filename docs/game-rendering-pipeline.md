# 游戏原生渲染管线调查

调查对象：`World of Warships` 客户端 `res_packages/shaders_0001.pkg` 里的编译着色器
（`*.win.dx11.fxo`），以及导出器 `wows-gltf-exporter` 输出的 GLB。

用途：让 Blender 渲染脚本按游戏的方式着色，贴图通道语义与分析结论都记录在此。

## 1. 游戏用的是什么材质模型

舰船走 **PBS**（Physically Based Shading，基于物理的着色）管线：

```
shaders/materials/pbs/ship_material.win.dx11.fxo          # 常规舰船
shaders/materials/pbs/ship_material_skinned.win.dx11.fxo  # 炮塔等蒙皮件
shaders/materials/pbs/ship_camo_material*.fxo             # 迷彩
shaders/materials/pbs/ship_emissive_material.fxo          # 自发光
```

> `shaders/std_effects/PBS_ship.win.dx11.fxo` 是旧版/低配路径，变量里带
> `g_legacySpecularPow`、`g_legacyAlbedoToSpecular`，用于把老材质迁移到 PBS。

## 2. 贴图通道语义（关键）

从 `ship_material` 的贴图槽注释里直接读到：

| 槽位变量 | 官方注释 | 含义 |
|---|---|---|
| `diffuseMap` | The diffuse map for the material | 漫反射（颜色） |
| `normalMap` | The normal map for the material | 法线 |
| `metallicGlossMap` | **The metallic and gloss map for the material** | 金属度 + 光泽度 |
| `detailMap` | Detail normal(rg), albedo(a) map | 细节法线 + 细节颜色 |
| `ambientOcclusionMap` | The ambient occlusion map for the material | AO |

### _mg（金属光泽）的通道打包

用像素数据实测确定（不能只看颜色猜）：

| 区域 | 占比 | mg.R | mg.G | mg.B |
|---|---|---|---|---|
| 灰色油漆 | 52.6% | 0.393 | 0.262 | **0.380** |
| 锈蚀 | 26.1% | 0.254 | 0.179 | **0.001** |
| 木质甲板 | 29.5% | 0.243 | 0.178 | **0.002** |
| 红船底 | 4.6% | 0.336 | 0.240 | **0.000** |

结论：**B = 光泽度**（油漆光滑 ≈0.38，锈蚀/木材/涂漆粗糙 ≈0），
**R = 金属度**（G 与 R 高度一致，作为备份）。
`render_ship.py` 用 `metallic = R`、`roughness = 1 - B`。

### _n（法线）：细节在 B 通道

实测这张贴图的通道方差：

| 通道 | 均值 | 标准差 |
|---|---|---|
| R | 0.503 | 0.025 |
| G | 0.503 | 0.042 |
| **B** | 0.450 | **0.132** |

**B 的标准差是 R/G 的 3–5 倍，表面细节几乎全在 B 里**，R/G 基本平坦。

- 因此 `render_ship.py` 默认用 `--normal-mode bump`：把 B 当作高度场求法线，
  强度 `--normal-strength`（默认 0.10）。
- 曾经走过弯路：误判成"R/G 双通道法线，用 `z = sqrt(1-x²-y²)` 重建"。
  那样等于把几乎不变的 R/G 噪声放大成法线，船体侧面出现横向波纹和斑块
  （用户反馈的"坑洼"），已由 `--normal-mode rg` 保留为可选、不再默认。

## 3. 影响观感的全局参数

`$Globals` 里的 `g_pbsExtras (PbsExtras)`：

```
microShadowsIntensity / microShadowsIntensityShips   微观阴影（舰船单独一份）
specularMult           aoPower   soPower
overallWetness         indirectMultShips             舰船间接光倍率（单独一份）
lakeSpecularIntensity  pmremSupressByShadow  wetnessColor
```

`indirectMultShips` 和 `microShadowsIntensityShips` 带 `Ships` 后缀，说明游戏对舰船
的间接光单独做了补偿——这也解释了为什么接入 PBS 贴图后画面会变暗约 7 级，
需要在曝光上补回来（见第 5 节）。

色调映射 `g_tonemapParams (TonemapParams)`：

```
exposure  gtContrast  gtLinearSectionStart  gtLinearSectionLength  gtBlack
```

即 **GT（Guerrilla Tonemap）** 曲线：黑位、线性段起点/长度、对比度，
由 `tonemap_lut_bake` 烘焙成 LUT，`wg_hdr_bloom` 应用；另有自动曝光
`wg_average_luminance`。

光照为**单方向光** `g_sunLight` + PMREM 环境光（`g_PMREMTex`、`g_envBRDFTex`、
`g_GlobalREMSH` 球谐），**没有补光灯**。

## 4. 资源定位规则

贴图在游戏包里按 `<nation>/ship/<type>/textures/` 存放：

```
content/gameplay/germany/ship/battleship/textures/
```

命名有四种变体（提取脚本已全部兼容，匹配时不区分大小写、忽略下划线）：

| 布局 | 例 |
|---|---|
| `<ship>_hull_a` / `<ship>_DeckHouse_a` | Bismarck |
| `<ship>_Hull_a` / `<ship>_Deck_House_a` | Kongo |
| `<ship>_a`（整船一套） | Emden |

**资源目录名无法从舰船 Key 推导**（`PJSB007` → `JSB007` 是巧合，
`PGSC502` → `GSC020` 就不是），必须读模型路径。`extract_ship_textures.py`
通过 `wows-gltf-exporter -v` 输出的 `hull model:` 一行自动解析。

DDS 存储：每个 mip 层是**独立的完整 DDS 文件**——`.dd0` 是最大层，
`.dd1`/`.dd2` 依次递减，`.dds` 是最小层的尾巴。取 `.dd0` 即得原图。

## 5. 本仓库的还原做法

`extract_ship_textures.py` 把 a/mg/n/ao 导出为 PNG，`render_ship.py --textures`
按上面的通道语义接进 Principled BSDF：

| 游戏槽位 | Blender 接线 |
|---|---|
| diffuseMap | Base Color（导出器已接好） |
| metallicGlossMap.R | Metallic |
| metallicGlossMap.B | Roughness = `1 - B`，再映射到 `[roughness-min, roughness-max]` |
| normalMap | **B 通道当高度场**，走 Bump 节点（`--normal-mode bump`） |
| ambientOcclusionMap | 以 0.72 为中性点重映射后乘到颜色上（`--ao-strength` / `--ao-pivot`） |
| detailMap | **游戏中没有这张贴图**；程序化替代默认关闭（`--detail`，全船距离下会显成麻点） |

接入后船体会变暗约 7 级（法线微阴影 + 金属度分流），对应游戏里的
`indirectMultShips` 补偿，因此启用 `--textures` 时自动加 `PBS_EXPOSURE = 0.45`。

### 光照

游戏是**单方向光 + PMREM 环境**，没有补光灯。渲染脚本默认按此设置：

- 不创建补光灯，改为提高环境强度（`--env-boost`，默认 1.45）来撑起暗部
- 需要旧的补光效果时加 `--fill-light`

### 色调映射（GT 曲线）

用 `CompositorNodeCurveRGB` 近似 `g_tonemapParams`，参数一一对应：

| 参数 | 渲染脚本开关 |
|---|---|
| `gtLinearSectionStart` | `--gt-linear-start`（默认 0.08） |
| `gtLinearSectionLength` | `--gt-linear-len`（默认 0.52） |
| `gtContrast` | `--gt-contrast`（默认 1.18） |
| `gtBlack` | `--gt-black` |

启用：`--gt-tonemap`（**默认开启**，`--no-gt-tonemap` 关闭）。

### 自动曝光

`wg_average_luminance` 的替代实现：每个视角先渲一张 1/16 分辨率的低采样
探针图，取画面亮度中位数，算出相对目标 key 的档位差再施加到该视角曝光上。
中位数而非均值，避免大片亮天空把每个视角都压暗。

| 参数 | 说明 |
|---|---|
| `--auto-exposure` | 默认开启，`--no-auto-exposure` 关闭 |
| `--auto-key` | 测光目标亮度（默认 0.18） |
| `--auto-strength` | 修正量的应用比例（默认 0.35，模拟游戏渐进适应） |
| `--auto-max` | 单视角修正上限（默认 0.8 档） |

### 迷彩

游戏 `ship_camo_material` 的贴图槽是 **`camoColorMask`**：它不是彩绘图案，
而是 R/G/B 三个通道分别索引三块涂装区（黑色 = 不涂）。渲染时按通道值把
对应色板颜色混合到基础颜色上。

- 提取：`extract_ship_textures.py --camo`，输出 `<船>_<部件>_camo_<NN>.png`
- 使用：`--camo 01`（配色板 `--camo-colors`，9 个数 = 3 组 RGB）
- 平铺：`--camo-repeats`

### 输出文件名

输出为扁平结构（仅国家一层目录），文件名自发描述：

```
WoWs_Renders/<国家>/<船Key>_<舰种>_T<级别>_<视角>_<宽>x<高>.png
```

例如 `PGSB108_Bismarck_Battleship_T8_hero_3840x2160.png`。舰种用单数形式，
超级战舰级别记为 `TStar`。不同分辨率的渲染可并存，
`render_all.ps1 -SkipExisting` 按相同前缀与分辨率后缀判断是否已存在。

**实测得到的两条重要结论**（踩过坑，记录避免重犯）：

1. 合成器里的 CurveRGB 作用于**已经过显示变换的值**（sRGB 编码空间）。
   实测：恒等曲线与不加曲线输出完全一致（都是 0.7333）；线性 0.5 的
   自发光对应显示值 ~0.735。所以它是一条**显示空间对比度曲线**，
   必须锚定在 (0,0)/(1,1) 之间，不能当作场景线性 tonemap 使用。
2. `curve.points.new()` 会与默认的 (0,0)/(1,1) 端点**重复**，重复端点会让
   曲线求值错误——实测天空被推到纯白（255）。正确做法是只移除中间点，
   端点保持 Blender 自带的那两个。

对比图：`docs/compare/lighting-normal.png`、`docs/compare/pbs-final.png`。

## 6. 尚未还原的部分

以下是仍然没有完全对齐游戏的地方，按影响从大到小：

- **camoColor 色板数值**：迷彩遮罩（`camoColorMask`，R/G/B 索引涂装区）
  已经能解码并着色。但**真实色板始终没有找到**，排查过：
  - `content/GameParams.data`（可提取，`zlib.decompress(data[::-1])` + pickle，
    8879 万字节解码成功）里，228 个 `camo_*` 方案编号**没有任何一个是顶层条目**；
  - `content/assets.bin`（183 MB，可提取）里只有 `camoColor0..3` 这几个**字段名**
    和材质名，没有逐方案的色值。
  结论：色板要求解二进制格式，目前用近似军灰色板（`--camo-colors` 可覆盖）。
- **detailMap 贴图**：游戏中不存在（德/日/法/美各舰种实测只有 a/mg/n/ao
  四种后缀，共享目录只有一个 3.4KB 的 `ship_atlas_detail.dds` mip 尾巴）。
  目前用程序化 detail normal 近似同一效果。
- **自动曝光的精确曲线**：`wg_average_luminance` 的具体参数（帧级配置）
  未从资源中取出，现按"测光中位数 + 目标 key + 限幅"实现，
  默认只应用 35% 修正量，见 `--auto-exposure` / `--auto-key` / `--auto-strength`。
- **PMREM 环境光**：用 Blender 物理天空近似，未使用游戏的 PMREM 贴图。
  `specularMult` / `overallWetness` 已按参数名接入（`--specular-mult` / `--wetness`），
  但游戏在各场景下的具体取值未取得。
