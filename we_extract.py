#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Wallpaper Engine 创意工坊壁纸 → 能直接用的视频 / 图片。

    python3 we_extract.py                      # 什么都不给 = 把订阅的壁纸全部导出
    python3 we_extract.py 3660962877           # 只导出这一项
    python3 we_extract.py --list               # 看看订阅了哪些
    python3 we_extract.py --list --json        # 给别的程序读
    python3 we_extract.py --all --to-hidamari  # 全部导出，视频顺手拷进 Hidamari 目录
    python3 we_extract.py --steam "D:\\Steam"   # 手动指定 Steam 根目录（找不到时用）
    python3 we_extract.py --check 3660962877   # 只体检，不写盘

跨平台：Windows / macOS / Linux 都能跑，**只用 Python 标准库**（LZ4 解压、
DXT 贴图解码、写 PNG 全是自己实现的），不需要 pip install 任何东西。

设计原则：每一步都自校验；拿不准的格式**原样导出并标注**，绝不瞎猜。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import zlib

__version__ = "2.0"

WE_APPID = "431960"                       # Wallpaper Engine
HIDAMARI_VIDEO_DIR = os.path.expanduser("~/视频/Hidamari")
DEFAULT_OUT = os.path.join(
    os.path.expanduser("~"),
    "Pictures" if sys.platform == "win32" else "图片",
    "WallpaperEngine",
)

# Windows 的控制台默认是 GBK，打印中文文件名会炸，先把标准输出掰成 UTF-8
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def log(msg=""):
    print(msg, flush=True)


# ==================================================================== 找工坊目录
def _steam_roots():
    """尽量把所有可能的 Steam 安装位置都找出来"""
    home = os.path.expanduser("~")
    roots = []
    if sys.platform == "win32":
        try:
            import winreg
            for hive, key in ((winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"),
                              (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam")):
                try:
                    with winreg.OpenKey(hive, key) as k:
                        for name in ("SteamPath", "InstallPath"):
                            try:
                                roots.append(winreg.QueryValueEx(k, name)[0])
                            except OSError:
                                pass
                except OSError:
                    pass
        except Exception:
            pass
        for env in ("ProgramFiles(x86)", "ProgramFiles"):
            v = os.environ.get(env)
            if v:
                roots.append(os.path.join(v, "Steam"))
        drive = os.environ.get("SystemDrive", "C:")
        roots.append(os.path.join(drive + os.sep, "Steam"))
    elif sys.platform == "darwin":
        roots += [os.path.join(home, "Library/Application Support/Steam")]
    else:
        roots += [
            os.path.join(home, "snap/steam/common/.local/share/Steam"),   # snap 版
            os.path.join(home, ".steam/steam"),
            os.path.join(home, ".steam/root"),
            os.path.join(home, ".local/share/Steam"),
            os.path.join(home, ".var/app/com.valvesoftware.Steam/data/Steam"),
            os.path.join(home, ".var/app/com.valvesoftware.Steam/.local/share/Steam"),
        ]
    # 去重、保序、只留真实存在的
    seen, out = set(), []
    for r in roots:
        if not r:
            continue
        r = os.path.normpath(r)
        if r.lower() in seen or not os.path.isdir(r):
            continue
        seen.add(r.lower())
        out.append(r)
    return out


def _libraries_from_vdf(root):
    """从 steamapps/libraryfolders.vdf 里挖出其它盘上的库目录"""
    vdf = os.path.join(root, "steamapps", "libraryfolders.vdf")
    if not os.path.isfile(vdf):
        return []
    try:
        with open(vdf, "r", encoding="utf-8", errors="replace") as f:
            txt = f.read()
    except OSError:
        return []
    return [p.replace("\\\\", "\\") for p in re.findall(r'"path"\s+"([^"]+)"', txt)]


def find_workshop(explicit=None):
    """返回 .../workshop/content/431960 的绝对路径"""
    if explicit:
        p = os.path.abspath(os.path.expanduser(os.path.expandvars(explicit)))
        for cand in (os.path.join(p, "steamapps", "workshop", "content", WE_APPID),
                     os.path.join(p, "workshop", "content", WE_APPID),
                     os.path.join(p, "content", WE_APPID),
                     os.path.join(p, WE_APPID),
                     p):        # 直接给到 431960 这一层也认
            if os.path.isdir(cand):
                return cand
        sys.exit(f"指定的目录里找不到 workshop/content/{WE_APPID}：{p}")
    if os.environ.get("WE_WORKSHOP"):
        p = os.path.expanduser(os.environ["WE_WORKSHOP"])
        if os.path.isdir(p):
            return p

    roots = _steam_roots()
    cands = []
    for r in roots:
        cands.append(os.path.join(r, "steamapps", "workshop", "content", WE_APPID))
        for lib in _libraries_from_vdf(r):
            cands.append(os.path.join(lib, "steamapps", "workshop", "content", WE_APPID))
    for c in cands:
        if os.path.isdir(c):
            return os.path.normpath(c)
    sys.exit("找不到 Wallpaper Engine 的创意工坊目录。\n"
             "  请用 --steam <Steam 根目录> 指定，或设环境变量 WE_WORKSHOP=<.../workshop/content/431960>\n"
             f"  找过这些位置：\n    " + "\n    ".join(roots or ["(没找到任何 Steam 安装)"]))


def resolve(target, steam=None):
    root = find_workshop(steam)
    path = target if os.path.isabs(str(target)) else os.path.join(root, str(target))
    if not os.path.isdir(path):
        sys.exit(f"没有这个订阅项：{path}")
    return path, load_project(path)


def load_project(path):
    pj = os.path.join(path, "project.json")
    if not os.path.isfile(pj):
        return None
    for enc in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            with open(pj, "r", encoding=enc, errors="replace") as f:
                return json.load(f)
        except Exception:
            continue
    return None


def workshop_items(steam=None):
    root = find_workshop(steam)
    rows = []
    for name in sorted(os.listdir(root), key=lambda s: (len(s), s)):
        d = os.path.join(root, name)
        pj = load_project(d)
        if pj:
            rows.append({"id": name, "type": pj.get("type", "?"),
                         "title": pj.get("title", "?"), "dir": d})
    return root, rows


def safe_name(s):
    bad = '/\\:*?"<>|\n\r\t'
    return ("".join("_" if c in bad else c for c in s).strip() or "wallpaper")[:120]


# ============================================================ 容器 / 贴图 解析
def parse_pkg(d):
    """scene.pkg → (magic, [(name, off, size)], data_start)

    布局：[u32 magic长度=8]["PKGV00xx"][u32 条目数]
          每条 [u32 路径长][路径 UTF-8][u32 偏移][u32 长度]
    偏移相对 data_start（= 条目表结束处）。
    自检：所有 size 之和 == 文件长度 - 表长。
    """
    off = 0
    (mlen,) = struct.unpack_from("<I", d, off)
    off += 4
    magic = d[off:off + mlen].decode("ascii", "replace")
    off += mlen
    if not magic.startswith("PKGV"):
        raise ValueError(f"不是 PKGV 容器：magic={magic!r}")
    (count,) = struct.unpack_from("<I", d, off)
    off += 4
    entries = []
    for _ in range(count):
        (l,) = struct.unpack_from("<I", d, off)
        off += 4
        name = d[off:off + l].decode("utf-8", "replace")
        off += l
        o, sz = struct.unpack_from("<II", d, off)
        off += 8
        entries.append((name, o, sz))
    return magic, entries, off


def parse_tex(d):
    """TEXV0005 贴图 → dict；不是 TEX 头就返回 None。

    头：  "TEXV0005\\0" "TEXI0001\\0" + 7×i32（格式/标志/宽/高/宽2/高2/未知）
    体：  "TEXB0003\\0" 或 "TEXB0004\\0"
          [i32 图像数]
          TEXB0004 比 0003 多一个 i32（实测恒为 0，直接跳过）
          每图 [i32 格式(-1=继承头部)] [i32 mip 数]
          每 mip [i32 宽][i32 高][i32 压缩(0=原样,1=LZ4)][i32 解压后大小][i32 数据长][数据]
    """
    if not d.startswith(b"TEXV"):
        return None
    o = 0

    def rstr():
        nonlocal o
        e = d.index(b"\0", o)
        s = d[o:e].decode("ascii", "replace")
        o = e + 1
        return s

    ver = rstr()
    rstr()                                            # TEXI0001
    fmt, flags, w, h, w2, h2, unk = struct.unpack_from("<7i", d, o)
    o += 28
    tb = rstr()
    m = re.fullmatch(r"TEXB(\d+)", tb)
    if not m:
        return None
    tbver = int(m.group(1))
    (nimg,) = struct.unpack_from("<i", d, o)
    o += 4
    images = []
    for _ in range(nimg):
        (ifmt,) = struct.unpack_from("<i", d, o)
        o += 4
        if tbver >= 4:
            o += 4                                    # TEXB0004 多出的那个 i32
        (nmip,) = struct.unpack_from("<i", d, o)
        o += 4
        mips = []
        for _ in range(nmip):
            mw, mh, comp, unc, ml = struct.unpack_from("<iiiii", d, o)
            o += 20
            mips.append(dict(w=mw, h=mh, comp=comp, unc=unc, size=ml, off=o))
            o += ml
        images.append(dict(fmt=ifmt, mips=mips))
    return dict(ver=ver, tb=tb, tbver=tbver, fmt=fmt, flags=flags, w=w, h=h,
                images=images, consumed=o, total=len(d))


# ------------------------------------------------------------------ LZ4 解压
def lz4_raw(src):
    """LZ4 块格式（没有帧头）解压。WE 的 .tex 有些 mip 就是这种。"""
    out = bytearray()
    i, n = 0, len(src)
    while i < n:
        tok = src[i]
        i += 1
        ll = tok >> 4
        if ll == 15:
            while True:
                b = src[i]
                i += 1
                ll += b
                if b != 255:
                    break
        out += src[i:i + ll]
        i += ll
        if i >= n:                                    # 末段只有字面量
            break
        off = src[i] | (src[i + 1] << 8)
        i += 2
        ml = (tok & 0xF) + 4
        if (tok & 0xF) == 15:
            while True:
                b = src[i]
                i += 1
                ml += b
                if b != 255:
                    break
        start = len(out) - off
        if start < 0:
            raise ValueError("LZ4 偏移越界")
        for k in range(ml):
            out.append(out[start + k])
    return bytes(out)


# ------------------------------------------------------- 完整文件（不用解压）
FILE_MAGICS = (
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
    (b"DDS ", ".dds"),
    (b"BM", ".bmp"),
)


def detect_file(b):
    """payload 本身就是个完整文件吗？返回扩展名（含点）或 None"""
    for sig, ext in FILE_MAGICS:
        if b.startswith(sig):
            return ext
    if len(b) > 12 and b[4:8] == b"ftyp":             # ISO BMFF：mp4 / mov / m4v
        brand = b[8:12]
        return ".mov" if brand.startswith(b"qt") else ".mp4"
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return ".webp"
    if b[:4] == b"RIFF" and b[8:12] == b"AVI ":
        return ".avi"
    return None


# ------------------------------------------------------------- 贴图格式表
# 已实测坐实的头部格式码（别人的包里可能还有别的码，拿不准就按每像素字节数推断）
KNOWN_FMT = {
    0: ("RGBA8888", "RGBA"),
    4: ("DXT5/BC3", "bc3"),
    9: ("R8", "L"),
}


def layout_for(hdr_fmt, unc, w, h):
    """返回 (布局名, 解出的模式)。先看已知格式码，再看 每像素字节数。"""
    px = max(1, w * h)
    bpp = unc / px if px else 0
    if hdr_fmt in KNOWN_FMT:
        name, mode = KNOWN_FMT[hdr_fmt]
        want = {"RGBA": 4.0, "bc3": 1.0, "bc1": 0.5, "L": 1.0, "LA": 2.0}[mode]
        if abs(bpp - want) < 0.01:
            return name, mode
        return f"{name}?尺寸不符({bpp:g}B/px)", None
    if abs(bpp - 4.0) < 0.01:
        return f"按 4B/px 推断为 RGBA8888（格式码 {hdr_fmt} 未知）", "RGBA"
    if abs(bpp - 2.0) < 0.01:
        return f"按 2B/px 推断为双通道（格式码 {hdr_fmt} 未知）", "LA"
    if abs(bpp - 1.0) < 0.01:
        return f"按 1B/px 推断为 DXT5/BC3（格式码 {hdr_fmt} 未知）", "bc3"
    if abs(bpp - 0.5) < 0.01:
        return f"按 0.5B/px 推断为 DXT1/BC1（格式码 {hdr_fmt} 未知）", "bc1"
    return f"无法判断（格式码 {hdr_fmt}，{bpp:g}B/px）", None


# --------------------------------------------------------- DXT/BC 块解码
def _c565(v):
    return (((v >> 11) & 31) * 255 // 31, ((v >> 5) & 63) * 255 // 63, (v & 31) * 255 // 31)


def _bc1_colors(blk, o, force4=False):
    c0, c1, idx = struct.unpack_from("<HHI", blk, o)
    a = [_c565(c0), _c565(c1)]
    if c0 > c1 or force4:
        a += [tuple((2 * a[0][k] + a[1][k]) // 3 for k in range(3)),
              tuple((a[0][k] + 2 * a[1][k]) // 3 for k in range(3))]
        opaque3 = True
    else:
        a += [tuple((a[0][k] + a[1][k]) // 2 for k in range(3))]
        opaque3 = False
    out = []
    for i in range(16):
        c = (idx >> (2 * i)) & 3
        if c == 3 and not opaque3:
            out.append((0, 0, 0, 0))
        else:
            r, g, b = a[c]
            out.append((r, g, b, 255))
    return out


def _alpha_bc3(blk):
    a0, a1 = blk[0], blk[1]
    bits = int.from_bytes(blk[2:8], "little")
    if a0 > a1:
        tab = [a0, a1] + [((7 - i) * a0 + i * a1) // 7 for i in range(1, 7)]
    else:
        tab = [a0, a1] + [((5 - i) * a0 + i * a1) // 5 for i in range(1, 5)] + [0, 255]
    return [tab[(bits >> (3 * i)) & 7] for i in range(16)]


def decode_bc(raw, w, h, kind):
    """把 BC1/BC2/BC3 裸数据解成 RGBA 字节串；数据长度不对返回 None"""
    bw, bh = (w + 3) // 4, (h + 3) // 4
    need = bw * bh * (8 if kind == "bc1" else 16)
    if len(raw) < need:
        return None
    out = bytearray(w * h * 4)
    p = 0
    for by in range(bh):
        for bx in range(bw):
            blk = raw[p:p + (8 if kind == "bc1" else 16)]
            p += len(blk)
            if kind == "bc3":
                al = _alpha_bc3(blk[:8])
                cols = _bc1_colors(blk, 8, force4=True)
                px = [(cols[i][0], cols[i][1], cols[i][2], al[i]) for i in range(16)]
            elif kind == "bc2":
                nib = int.from_bytes(blk[0:8], "little")
                cols = _bc1_colors(blk, 8, force4=True)
                px = [(cols[i][0], cols[i][1], cols[i][2], ((nib >> (4 * i)) & 15) * 17)
                      for i in range(16)]
            else:
                px = _bc1_colors(blk, 0)
            for j in range(4):
                y = by * 4 + j
                if y >= h:
                    break
                for i in range(4):
                    x = bx * 4 + i
                    if x >= w:
                        continue
                    r, g, b, a = px[j * 4 + i]
                    o = (y * w + x) * 4
                    out[o:o + 4] = bytes((r, g, b, a))
    return bytes(out)


# ------------------------------------------------------------- 写 PNG
_PNG_CH = {"L": 1, "LA": 2, "RGB": 3, "RGBA": 4}


def write_png(path, w, h, data, mode):
    ch = _PNG_CH[mode]
    stride = w * ch
    if len(data) < stride * h:
        raise ValueError("像素数据不够")
    rows = bytearray()
    for y in range(h):
        rows.append(0)                                # 每行的过滤器类型：None
        rows += data[y * stride:(y + 1) * stride]
    ctype = {"L": 0, "RGB": 2, "LA": 4, "RGBA": 6}[mode]

    def chunk(tag, payload):
        c = struct.pack(">I", len(payload)) + tag + payload
        return c + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, ctype, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(bytes(rows), 6))
           + chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(png)


# ============================================================ mip → 产物
def mip_payload(texdata, mip):
    """返回 (kind, 数据, 扩展名)
    kind: 'file' 本身就是完整文件 / 'raw' 裸像素 / 'unknown' 拿不准
    """
    raw = texdata[mip["off"]:mip["off"] + mip["size"]]
    ext = detect_file(raw)
    if ext:
        return "file", raw, ext
    if mip["comp"] == 1:
        try:
            dec = lz4_raw(raw)
        except Exception:
            return "unknown", raw, ".bin"
        if mip["unc"] and len(dec) == mip["unc"]:
            return "raw", dec, None
        return "unknown", raw, ".bin"
    if mip["unc"] and len(raw) == mip["unc"]:
        return "raw", raw, None
    return "unknown", raw, ".bin"


def describe_mips(tex):
    """给 info 用：每个 mip 一句话"""
    lines = []
    for i, mip in enumerate(tex["images"][0]["mips"]):
        px = max(1, mip["w"] * mip["h"])
        bpp = mip["unc"] / px if px else 0
        lines.append(f"mip{i} {mip['w']}x{mip['h']} 压缩={mip['comp']} "
                     f"解压后={mip['unc']} 存了={mip['size']} ({bpp:g}B/px)")
    return lines


def extract_tex(texdata, outdir, relname, want_mips=("first",), dry=False):
    """把一个 .tex 导出；返回 [(路径, 说明)]"""
    tex = parse_tex(texdata)
    if tex is None:
        if dry:
            return []
        p = os.path.join(outdir, relname + ".bin")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(texdata)
        return [(p, "不是 TEX 头，原样导出")]

    if tex["consumed"] != tex["total"]:
        log(f"    ! TEX 解析没走完（{tex['consumed']}/{tex['total']}），可能格式有变")

    got = []
    stem = relname[:-4] if relname.lower().endswith(".tex") else relname
    mips = tex["images"][0]["mips"]
    idxs = range(len(mips)) if "all" in want_mips else ([0] if mips else [])

    for i in idxs:
        mip = mips[i]
        kind, payload, ext = mip_payload(texdata, mip)
        suffix = "" if len(mips) == 1 or i == 0 else f"_mip{i}"
        if kind == "file":
            name = safe_name(os.path.basename(stem)) + suffix + (ext or ".bin")
            note = f"是完整文件（{ext}）"
        elif kind == "raw":
            layout, mode = layout_for(tex["fmt"], mip["unc"], mip["w"], mip["h"])
            name = safe_name(os.path.basename(stem)) + suffix + ".png"
            note = layout
            if mode is None:
                name = safe_name(os.path.basename(stem)) + suffix + ".bin"
                note += " → 原样导出"
        else:
            name = safe_name(os.path.basename(stem)) + suffix + ".bin"
            note = "拿不准格式，原样导出"
            mode = None

        if dry:
            got.append((name, note))
            continue
        os.makedirs(outdir, exist_ok=True)
        p = os.path.join(outdir, name)
        if kind == "raw" and mode:
            if mode in ("bc1", "bc3", "bc2"):
                rgba = decode_bc(payload, mip["w"], mip["h"], mode)
                if rgba is None:
                    with open(p[:-4] + ".bin", "wb") as f:
                        f.write(payload)
                    got.append((p[:-4] + ".bin", note + " → 数据长度不符，原样导出"))
                    continue
                write_png(p, mip["w"], mip["h"], rgba, "RGBA")
            else:
                ch = _PNG_CH[mode]
                need = mip["w"] * mip["h"] * ch
                write_png(p, mip["w"], mip["h"], payload[:need], mode)
        else:
            with open(p, "wb") as f:
                f.write(payload)
        got.append((p, note))
    return got


# ============================================================ 子命令
def cmd_list(a):
    root, rows = workshop_items(a.steam)
    if a.json:
        log(json.dumps({"workshop_dir": root, "items": rows}, ensure_ascii=False, indent=2))
        return
    for r in rows:
        log(f"  {r['id']}  [{r['type']:5}]  {r['title']}")
    log(f"\n共 {len(rows)} 项，源目录 {root}")


def cmd_info(a):
    path, pj = resolve(a.target, a.steam)
    log(f"目录：{path}")
    if not pj:
        sys.exit("  没有 project.json，不像是工坊壁纸")
    typ = pj.get("type", "?")
    log(f"类型：{typ}")
    log(f"标题：{pj.get('title')}")
    log(f"文件：{sorted(os.listdir(path))}")

    if typ == "video":
        vids = [f for f in os.listdir(path)
                if f.lower().endswith((".mp4", ".webm", ".mkv", ".avi", ".mov"))]
        log(f"  → 目录里直接就是视频：{vids}（无需解包，拷出来即可）")
        return
    if typ not in ("scene", "web"):
        return
    pkg = os.path.join(path, "scene.pkg")
    if not os.path.isfile(pkg):
        others = [f for f in os.listdir(path) if f.lower().endswith((".pkg", ".pak"))]
        log(f"  没有 scene.pkg（{others or '也没找到别的包'}）")
        return
    with open(pkg, "rb") as f:
        d = f.read()
    magic, entries, off = parse_pkg(d)
    total = sum(s for _, _, s in entries)
    log(f"  {magic} 容器：{len(entries)} 条目，数据区 {len(d) - off} 字节，"
        f"条目合计 {total} 字节，差 {len(d) - off - total}")
    for name, eo, es in entries:
        if not name.lower().endswith(".tex"):
            continue
        body = d[off + eo: off + eo + es]
        tex = parse_tex(body)
        if tex is None:
            log(f"    {es:>10}  {name}\n              不是 TEX 头（{body[:12]!r}）")
            continue
        log(f"    {es:>10}  {name}")
        log(f"              头：{tex['ver']}/{tex['tb']} 格式码={tex['fmt']} "
            f"{tex['w']}x{tex['h']} 图像数={len(tex['images'])}")
        for line in describe_mips(tex):
            log(f"              {line}")
        for i, mip in enumerate(tex["images"][0]["mips"][:1]):
            kind, _, ext = mip_payload(body, mip)
            if kind == "file":
                log(f"              → mip{i} 是完整文件：{ext}")
            elif kind == "raw":
                layout, mode = layout_for(tex["fmt"], mip["unc"], mip["w"], mip["h"])
                log(f"              → mip{i} LZ4 解压后按 {layout} 解 → "
                    f"{'可以出图' if mode else '解不了，原样导出'}")
            else:
                log(f"              → mip{i} 拿不准，将原样导出")


def _copy_previews(path, outdir):
    for f in sorted(os.listdir(path)):
        if f.lower().startswith("preview") or f.lower().endswith((".jpg", ".jpeg")):
            try:
                shutil.copy2(os.path.join(path, f), os.path.join(outdir, f))
            except OSError:
                pass


def do_extract(item_dir, pj, outroot, to_hidamari=False, keep_raw=True,
               want_mips=("first",), png_only=False):
    wid = os.path.basename(item_dir)
    title = (pj or {}).get("title", wid)
    typ = (pj or {}).get("type", "?")
    outdir = os.path.join(outroot, safe_name(wid))
    os.makedirs(outdir, exist_ok=True)
    log(f"\n=== {wid}  [{typ}]  {title}")

    videos = []

    if typ == "video":
        for f in sorted(os.listdir(item_dir)):
            if f.lower().endswith((".mp4", ".webm", ".mkv", ".avi", ".mov")):
                dst = os.path.join(outdir, safe_name(f))
                shutil.copy2(os.path.join(item_dir, f), dst)
                log(f"  视频 → {dst}")
                videos.append(dst)
        _copy_previews(item_dir, outdir)
        return videos

    pkg = os.path.join(item_dir, "scene.pkg")
    if not os.path.isfile(pkg):
        log("  没有 scene.pkg，跳过（web 类型或旧版分包）")
        return videos

    with open(pkg, "rb") as f:
        d = f.read()
    magic, entries, off = parse_pkg(d)
    log(f"  {magic}，{len(entries)} 条目")
    rawdir = os.path.join(outdir, "原始解包")

    # 大件优先，日志好看
    order = sorted(entries, key=lambda e: -e[2])
    for name, eo, es in order:
        body = d[off + eo: off + eo + es]
        if not name.lower().endswith(".tex"):
            if keep_raw:
                dst = os.path.join(rawdir, safe_name(name))
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                with open(dst, "wb") as f:
                    f.write(body)
            continue
        prod = extract_tex(body, outdir, name, want_mips=want_mips)
        for p, note in prod:
            log(f"  {os.path.basename(p)}  ←  {name}   [{note}]")
            if png_only and not p.lower().endswith(".png"):
                continue
            if p.lower().endswith((".mp4", ".mov", ".webm", ".mkv", ".avi", ".gif", ".webp")):
                videos.append(p)
        if keep_raw:
            dst = os.path.join(rawdir, safe_name(name))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(dst, "wb") as f:
                f.write(body)

    _copy_previews(item_dir, outdir)
    log(f"  产物目录：{outdir}")

    if to_hidamari and videos:
        if not os.path.isdir(HIDAMARI_VIDEO_DIR):
            log(f"  （没有 {HIDAMARI_VIDEO_DIR}，跳过拷贝；Hidamari 是 Linux 上的动态壁纸程序）")
        else:
            for v in videos:
                dst = os.path.join(HIDAMARI_VIDEO_DIR, os.path.basename(v))
                shutil.copy2(v, dst)
                log(f"  → Hidamari：{dst}")
    return videos


def cmd_extract(a):
    targets = []
    if a.all or not a.targets:
        root, rows = workshop_items(a.steam)
        if not rows:
            sys.exit(f"  {root} 下没有订阅项")
        targets = [(r["dir"], r) for r in rows]
    else:
        for t in a.targets:
            path, pj = resolve(t, a.steam)
            targets.append((path, pj))

    all_videos = []
    for path, pj in targets:
        all_videos += do_extract(path, pj, os.path.expanduser(a.out),
                                 to_hidamari=a.to_hidamari,
                                 keep_raw=not a.no_raw,
                                 want_mips=("all",) if a.all_mips else ("first",),
                                 png_only=a.png_only)
    log(f"\n共得到 {len(all_videos)} 个视频/动图：")
    for v in all_videos:
        log(f"  {v}")


def cmd_check(a):
    """不写盘，只体检"""
    targets = []
    if a.all or not a.targets:
        _, rows = workshop_items(a.steam)
        targets = [(r["dir"], r) for r in rows]
    else:
        for t in a.targets:
            path, pj = resolve(t, a.steam)
            targets.append((path, pj))
    for path, pj in targets:
        log(f"\n=== {os.path.basename(path)}  {(pj or {}).get('title','')}")
        pkg = os.path.join(path, "scene.pkg")
        if not os.path.isfile(pkg):
            log("  无 scene.pkg")
            continue
        with open(pkg, "rb") as f:
            d = f.read()
        magic, entries, off = parse_pkg(d)
        log(f"  {magic} {len(entries)} 条，长度自检差 {len(d) - off - sum(s for _,_,s in entries)}")
        for name, eo, es in entries:
            if not name.lower().endswith(".tex"):
                continue
            body = d[off + eo: off + eo + es]
            tex = parse_tex(body)
            if tex is None:
                log(f"  ! {name}: 不是 TEX 头")
                continue
            bad = ""
            if tex["consumed"] != tex["total"]:
                bad = f"  ! 解析只走了 {tex['consumed']}/{tex['total']}"
            kinds = []
            for mip in tex["images"][0]["mips"]:
                k, _, ext = mip_payload(body, mip)
                kinds.append({"file": ext or "文件", "raw": "可解", "unknown": "?"}[k])
            log(f"  {es:>10}  {name}\n             格式码={tex['fmt']} {tex['w']}x{tex['h']} "
                f"mips={len(tex['images'][0]['mips'])} → {', '.join(kinds)}{bad}")


def _find_ffmpeg():
    exe = shutil.which("ffmpeg")
    if exe:
        return [exe]
    if sys.platform.startswith("linux") and shutil.which("flatpak"):
        try:
            r = subprocess.run(["flatpak", "info", "io.github.jeffshee.Hidamari"],
                               capture_output=True, timeout=10)
            if r.returncode == 0:
                return ["flatpak", "run", "--command=ffmpeg", "io.github.jeffshee.Hidamari"]
        except Exception:
            pass
    return None


def cmd_still(a):
    """抽静帧。需要 ffmpeg（本机没有，就走 Hidamari 沙箱里的那个）"""
    src = os.path.abspath(os.path.expanduser(a.video))
    if not os.path.isfile(src):
        sys.exit(f"没有这个文件：{src}")
    ff = _find_ffmpeg()
    if not ff:
        sys.exit("找不到 ffmpeg。装一个（Windows: winget install Gyan.FFmpeg）再跑这个子命令。")
    out = os.path.abspath(os.path.expanduser(a.out)) if a.out else \
        os.path.splitext(src)[0] + (f"_{a.at:g}s" if a.at else "_帧") + ".png"
    cmd = ff + ["-y", "-ss", str(a.at), "-i", src, "-frames:v", "1"]
    if a.width:
        cmd += ["-vf", f"scale={a.width}:-1"]
    cmd += [out]
    log("  " + " ".join(cmd))
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit("ffmpeg 失败：\n" + (r.stderr or "")[-2000:])
    log(f"  → {out}")


# ==================================================================== main
SUBCMDS = {"list", "info", "extract", "still", "check"}


def build_parser():
    p = argparse.ArgumentParser(
        prog="we_extract.py", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"we_extract {__version__}")
    sub = p.add_subparsers(dest="cmd")

    def common(sp):
        sp.add_argument("--steam", help="Steam 根目录（或直接给 workshop/content/431960）")

    sp = sub.add_parser("list", help="列出所有订阅项")
    common(sp)
    sp.add_argument("--json", action="store_true", help="输出 JSON")
    sp.set_defaults(fn=cmd_list)

    sp = sub.add_parser("info", help="只报告，不写盘")
    common(sp)
    sp.add_argument("target", help="工坊 id 或目录")
    sp.set_defaults(fn=cmd_info)

    sp = sub.add_parser("check", help="批量体检（不写盘）")
    common(sp)
    sp.add_argument("targets", nargs="*", help="留空 + --all 表示全部")
    sp.add_argument("--all", action="store_true")
    sp.set_defaults(fn=cmd_check)

    sp = sub.add_parser("extract", help="解包（默认动作）")
    common(sp)
    sp.add_argument("targets", nargs="*", help="工坊 id 或目录；留空 = 全部")
    sp.add_argument("--all", action="store_true", help="全部订阅项")
    sp.add_argument("--out", default=DEFAULT_OUT, help=f"输出根目录（默认 {DEFAULT_OUT}）")
    sp.add_argument("--to-hidamari", action="store_true",
                    help="视频顺手拷进 ~/视频/Hidamari/（Linux 上 Hidamari 用）")
    sp.add_argument("--no-raw", action="store_true", help="不保留原始解包文件")
    sp.add_argument("--all-mips", action="store_true", help="每级 mip 都导出（默认只导最大的）")
    sp.add_argument("--png-only", action="store_true", help="只报图片，忽略视频")
    sp.set_defaults(fn=cmd_extract)

    sp = sub.add_parser("still", help="从视频里抽一帧（需要 ffmpeg）")
    sp.add_argument("video")
    sp.add_argument("--at", type=float, default=0.0, help="第几秒（默认 0）")
    sp.add_argument("--width", type=int, help="缩放宽度")
    sp.add_argument("--out", help="输出 png 路径")
    sp.set_defaults(fn=cmd_still)
    return p


ALIASES = {"--list": "list", "-l": "list", "--check": "check", "--info": "info"}


def main():
    argv = [ALIASES.get(t, t) for t in sys.argv[1:]]     # 顺手支持 --list / --check
    # 子命令挪到最前面，这样 `--steam X list` 和 `list --steam X` 都认
    for i, t in enumerate(argv):
        if t in SUBCMDS:
            argv = [t] + argv[:i] + argv[i + 1:]
            break
    else:
        argv = ["extract"] + argv                        # 没写子命令 = extract
    a = build_parser().parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
