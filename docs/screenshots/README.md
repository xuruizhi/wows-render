# 预览样图

本目录下的 JPEG 是 [README](../README.md) 顶部的展示样图，由本项目的渲染管线实际产出后压缩而来。

## 文件

| 文件 | 舰船 | 国家 / 舰种 / 级别 | 视角 | 预览尺寸 | 大小 |
|---|---|---|---|---|---|
| `01-kongo-hero.jpg` | 金刚 Kongō | R系 / 战列舰 / V | hero（主视角） | 1600×900 | 98 KB |
| `02-bismarck-hero.jpg` | 俾斯麦 Bismarck | D系 / 战列舰 / VIII | hero（主视角） | 1280×720 | 50 KB |
| `03-bismarck-side.jpg` | 俾斯麦 Bismarck | D系 / 战列舰 / VIII | side（正侧舷） | 1280×720 | 34 KB |
| `04-patrie-hero.jpg` | 祖国 Patrie | F系 / 战列舰 / ★ | hero（主视角） | 1600×900 | 66 KB |

合计约 247 KB。

## 怎么生成的

原始输出是 PNG，位于 `WoWs_Renders/<国家>/<舰种>/Tier <级别>/<船Key>/<视角>.png`（该目录不入库）。

压缩规则：保持长宽比缩放到不超过 1600 px 宽，转 JPEG，`quality=82`，开启 `optimize` 与 `progressive`。

```python
from PIL import Image
im = Image.open(src_png).convert("RGB")
if im.width > 1600:
    im = im.resize((1600, round(im.height * 1600 / im.width)), Image.LANCZOS)
im.save(dst_jpg, "JPEG", quality=82, optimize=True, progressive=True)
```

## 自己重新生成

```powershell
cd _render
.\render_all.ps1 -Ships PJSB007_Kongo_1942 -Res 3840x2160 -Samples 128 -Views hero
.\render_all.ps1 -Ships PGSB108_Bismarck -Res 1920x1080 -Samples 64 -Views hero,side
.\render_all.ps1 -Ships PFSB111_Patrie -Res 3840x2160 -Samples 128 -Views hero
```

## 版权

这些图是从游戏客户端模型渲染出的**衍生作品**，模型版权归 Wargaming.net 所有。
放在本仓库仅用于展示项目效果，不构成对游戏素材的再分发，也不表示 Wargaming 对本项目的认可。
如需在其他地方使用这些渲染图，请自行评估授权与合规性。
