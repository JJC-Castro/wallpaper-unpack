# we-unpack

把 **Wallpaper Engine**（Steam 创意工坊 AppID `431960`）里订阅的壁纸，解包成**能直接播的视频 / 能直接看的图片**。

- **单文件脚本，零依赖** —— 只用 Python 标准库。LZ4 解压、DXT 贴图解码、写 PNG 全是自己实现的，`pip install` 什么都不需要。
- **跨平台** —— Windows / macOS / Linux 都行，自动找 Steam 安装位置（Windows 会读注册表 + 扫 `libraryfolders.vdf`，所以装在 D 盘也找得到）。
- **自校验** —— 每一步都验证长度/尺寸，拿不准的格式**原样导出并标注**，绝不瞎猜。

```
python3 we_extract.py                     # 把订阅的壁纸全部导出
python3 we_extract.py --list              # 先看看订阅了哪些
python3 we_extract.py 3660962877          # 只导出这一项
```

Windows 上用 `py we_extract.py` 或 `python we_extract.py`。

---

## 一分钟上手

```bash
# 1. 看订阅了哪些（不需要写盘）
python3 we_extract.py --list

# 2. 全部导出（默认动作，什么都不用加）
python3 we_extract.py

# 3. 产物在哪
#    Windows : %USERPROFILE%\Pictures\WallpaperEngine\<id>\
#    macOS   : ~/Pictures/WallpaperEngine/<id>/
#    Linux   : ~/图片/WallpaperEngine/<id>/
```

导出的 `<id>` 目录里：

```
伊蕾娜 果园春色 有水印横屏.mp4     ← 正片（4K60 H.264）
伊蕾娜 提示框.png / 流星.png / …   ← 壁纸里的贴图（顺手解出来了）
preview.gif                        ← 创意工坊自带的预览图
原始解包/                          ← 包内所有原始文件，一个没丢（scene.json、着色器、音轨…）
```

## 全部命令

| 命令 | 作用 |
|---|---|
| `we_extract.py`（无参数） | 全部订阅项都导出 |
| `we_extract.py <id\|目录>` | 只导出指定的 |
| `we_extract.py --list [--json]` | 列出订阅项；`--json` 给别的程序读 |
| `we_extract.py info <id>` | 只报告包内结构，不写盘 |
| `we_extract.py check --all` | 批量体检（不写盘） |
| `we_extract.py still <视频> [--at 8] [--width 1920]` | 从视频里抽一帧（需要 ffmpeg） |

常用开关：

| 开关 | 说明 |
|---|---|
| `--out DIR` | 换个输出根目录 |
| `--steam DIR` | 手动指定 Steam 位置（给 Steam 根目录、`steamapps/workshop`、或者直接给 `431960` 那一层都认） |
| `--to-hidamari` | 视频顺手拷一份到 `~/视频/Hidamari/`（给 Linux 上的动态壁纸程序 [Hidamari](https://github.com/jeffshee/hidamari) 用） |
| `--no-raw` | 不保留 `原始解包/`（只要视频就加它） |
| `--all-mips` | 每级 mip 都导出（默认只导最大的那张） |

也可以用环境变量 `WE_WORKSHOP=<.../workshop/content/431960>` 指定工坊目录。

---

## 它是怎么工作的

Wallpaper Engine 的 `type: scene` 壁纸会把所有素材塞进一个 `scene.pkg`，素材又大多是私有格式。这个脚本把三层都拆了：

**1. `scene.pkg` —— PKGV 容器**

```
[u32 magic长度=8]["PKGV00xx"][u32 条目数]
每条 [u32 路径长][路径 UTF-8][u32 偏移][u32 长度]
```

偏移是相对**条目表结束处**的（不是文件开头，很容易搞错）。自检：所有条目长度之和 == 文件长度 − 表长。

**2. `.tex` —— TEXV0005 贴图，但里面装的经常不是贴图**

```
"TEXV0005\0" "TEXI0001\0" + 7×i32(格式/标志/宽/高/宽2/高2/未知)
"TEXB0003\0" 或 "TEXB0004\0"      ← 0004 比 0003 多一个 i32，得跳过
[i32 图像数]
每图 [i32 格式(-1=继承头部)] [i32 mip 数]
每 mip [i32 宽][i32 高][i32 压缩][i32 解压后大小][i32 数据长][数据]
```

关键在这里：**当 mip 是"原样不压缩"、且数据以 `00 00 00 xx 'ftyp'` 开头时，这段"贴图"就是一整段完整的 MP4 文件**。
所谓 4K 动态壁纸，其实就是一个视频贴图 + 一堆实时叠加的着色器（色调偏移、水波纹、泛光…）。
实测两例都是 **3840×2160 / 4000×2300 的 H.264 60fps + AAC**，直接改名成 `.mp4` 就能播。

同样是"原样不压缩"的还有 PNG（贴图带 alpha 时 WE 会直接存 PNG）。

**3. mip 数据是 LZ4 块压缩**

标记为"压缩"的 mip，是**没有帧头的裸 LZ4 块**。解压结果长度必须正好等于头里的"解压后大小"，对不上就是不认识，原样导出。

解压后按头部格式码解释：

| 格式码 | 含义 | 状态 |
|---|---|---|
| 0 | RGBA8888 | 已实测 |
| 4 | DXT5 / BC3 | 已实测 |
| 9 | R8（灰度） | 已实测 |
| 其它 | 按 每像素字节数 推断（4B→RGBA、1B→DXT5、0.5B→DXT1…） | 推断，会标注 |

> ⚠️ 网络上的 WE 格式码对照表（0=RGBA、1=DXT5、2=DXT3、3=DXT1、4=RG88、5=R8）**和实测对不上**——实测格式码 4 的贴图按 DXT5 解出来是完美图像，按 RG88 解则完全错位。这个脚本**以数据长度自校验为准**，不迷信对照表。

---

## 已知边界

- **`type: web` 的壁纸**（网页工程）不处理。
- **视频是"底片"**：拿到的 mp4 是壁纸的动态画面本身，但**没有** WE 实时叠加的那些着色器效果（色调、水波、泛光……）。做动态壁纸够用，但和 WE 里的观感会有差别。
- **纯贴图型 scene**（包里没有内嵌视频）只能出静态图，还原不出动画。
- **作者加的水印**（素材名里常写着"有水印"）是原画自带的，脚本不会去。
- 想要无损转封装/压缩，自己上 `ffmpeg`；脚本只负责把原始字节掏出来。

## 环境要求

- Python **3.7+**（用过 `sys.stdout.reconfigure`，3.7 起有）
- 其它什么都不用装。`still` 子命令需要 `ffmpeg`（Windows: `winget install Gyan.FFmpeg`）

## License

MIT（见 `LICENSE`）。

---

## English quick start

Unpack **Wallpaper Engine** workshop wallpapers (Steam AppID `431960`) into plain, playable
**MP4/PNG** files. Single file, **standard library only** — no `pip install`.

```bash
python3 we_extract.py --list      # what you're subscribed to
python3 we_extract.py             # extract everything
python3 we_extract.py 3660962877  # just this one
```

Steam is located automatically on Windows/macOS/Linux (Windows reads the registry and
`libraryfolders.vdf`, so non-default libraries work). Override with `--steam DIR`.

Output lands in `Pictures/WallpaperEngine/<id>/` (Linux: `~/图片/WallpaperEngine/<id>/`),
with the video on top, decoded textures beside it, and every original file preserved under
`原始解包/`.

Implemented from scratch here: the PKGV container parser, the TEXV0005 texture parser
(versions `TEXB0003`/`TEXB0004`), a raw **LZ4 block** decompressor, **BC1/BC2/BC3** (DXT)
decoders, and a dependency-free **PNG writer**. The cool part: a `.tex` is often **not a
texture at all** — it's a complete MP4 or PNG stored verbatim, which is how those 4K "scene"
wallpapers get their video.
