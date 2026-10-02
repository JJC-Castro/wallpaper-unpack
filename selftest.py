#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""we_extract.py 的自检 —— 不需要任何真实壁纸，自己造数据喂进去。

    python3 selftest.py

覆盖：PKGV 容器解析 / TEXV0005 解析（TEXB0003 与 0004）/ 裸 LZ4 块解压 /
      BC3(DXT5) 解码 / 无依赖 PNG 写出 / 端到端解包。
"""

import os
import struct
import sys
import tempfile
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import we_extract as W                                       # noqa: E402

FAILED = []


def check(name, cond, extra=""):
    print(f"  {'✓' if cond else '✗'} {name}{('  ' + extra) if extra else ''}")
    if not cond:
        FAILED.append(name)


# ------------------------------------------------------------------ 造数据
def lz4_literal(data):
    """造一个"只有字面量"的合法 LZ4 块（解压器只要读到最后就是这一块）"""
    n = len(data)
    out = bytearray()
    out.append(min(n, 15) << 4)
    if n >= 15:
        rest = n - 15
        while rest >= 255:
            out.append(255)
            rest -= 255
        out.append(rest)
    out += data
    return bytes(out)


def build_tex(fmt, w, h, mips):
    """mips: [(压缩标记, 数据, 解压后大小), ...]"""
    b = b"TEXV0005\0" + b"TEXI0001\0"
    b += struct.pack("<7i", fmt, 2, w, h, w, h, 0)
    b += b"TEXB0004\0" + struct.pack("<i", 1)              # 图像数
    b += struct.pack("<i", -1) + struct.pack("<i", 0)      # 格式继承 + 0004 多出来的那个 i32
    b += struct.pack("<i", len(mips))
    for comp, blob, unc in mips:
        b += struct.pack("<iiiii", w, h, comp, unc, len(blob)) + blob
    return b


def build_tex3(fmt, w, h, mips):
    """老版本 TEXB0003（没有多出来的那个 i32）"""
    b = b"TEXV0005\0" + b"TEXI0001\0"
    b += struct.pack("<7i", fmt, 2, w, h, w, h, 0)
    b += b"TEXB0003\0" + struct.pack("<i", 1)
    b += struct.pack("<i", -1) + struct.pack("<i", len(mips))
    for comp, blob, unc in mips:
        b += struct.pack("<iiiii", w, h, comp, unc, len(blob)) + blob
    return b


def build_pkg(magic, files):
    ent, data = b"", b""
    for name, blob in files:
        nb = name.encode("utf-8")
        ent += struct.pack("<I", len(nb)) + nb + struct.pack("<II", len(data), len(blob))
        data += blob
    return (struct.pack("<I", len(magic)) + magic.encode() + struct.pack("<I", len(files))
            + ent + data)


def bc3_block_solid(rgba):
    """造一个 4x4 全同色的 BC3 块"""
    r, g, b, a = rgba
    c = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
    return bytes((a, a)) + b"\0" * 6 + struct.pack("<HHI", c, c, 0)


# ------------------------------------------------------------------ 测试
def t_lz4():
    print("\n[1] 裸 LZ4 块解压")
    for n in (0, 1, 14, 15, 16, 255, 300, 70000):
        data = bytes((i * 37 + 11) % 256 for i in range(n))
        check(f"长度 {n} 往返一致", W.lz4_raw(lz4_literal(data)) == data)


def t_pkg():
    print("\n[2] PKGV 容器")
    files = [("a/b.txt", b"hello"), ("c.bin", bytes(range(256)))]
    d = build_pkg("PKGV0023", files)
    magic, entries, off = W.parse_pkg(d)
    check("magic 读对", magic == "PKGV0023")
    check("条目数读对", len(entries) == 2)
    check("长度自检差为 0", len(d) - off - sum(s for _, _, s in entries) == 0)
    ok = all(d[off + o: off + o + s] == blob for (n, o, s), (_, blob) in zip(entries, files))
    check("每个条目内容对得上", ok)


def t_tex():
    print("\n[3] TEXV0005 解析（003 与 004）")
    t4 = W.parse_tex(build_tex(4, 8, 8, [(1, b"x" * 10, 64)]))
    check("TEXB0004 解析成功", t4 is not None and t4["tbver"] == 4)
    check("TEXB0004 完全消费", t4 and t4["consumed"] == t4["total"])
    check("TEXB0004 mip 字段对", t4 and t4["images"][0]["mips"][0]["unc"] == 64)
    t3 = W.parse_tex(build_tex3(0, 8, 8, [(1, b"y" * 20, 256)]))
    check("TEXB0003 解析成功", t3 is not None and t3["tbver"] == 3)
    check("TEXB0003 完全消费", t3 and t3["consumed"] == t3["total"])


def t_formats():
    print("\n[4] 格式判定")
    check("格式码 0 → RGBA8888", W.layout_for(0, 4 * 4 * 4, 4, 4)[1] == "RGBA")
    check("格式码 4 → DXT5", W.layout_for(4, 4 * 4, 4, 4)[1] == "bc3")
    check("格式码 9 → 灰度", W.layout_for(9, 4 * 4, 4, 4)[1] == "L")
    check("未知码但 4B/px → 推断 RGBA", W.layout_for(77, 16 * 16 * 4, 16, 16)[1] == "RGBA")
    check("未知码且字节数离谱 → 拒绝", W.layout_for(77, 12345, 16, 16)[1] is None)
    check("格式码与尺寸打架 → 拒绝", W.layout_for(4, 16 * 16 * 4, 16, 16)[1] is None)


def t_bc3():
    print("\n[5] BC3(DXT5) 解码")
    blk = bc3_block_solid((255, 128, 0, 200))
    raw = blk * 16
    got = W.decode_bc(raw, 8, 8, "bc3")
    px = tuple(got[0:4])
    check("解出 8x8 RGBA", got is not None and len(got) == 8 * 8 * 4)
    check("颜色近似（565 量化）", abs(px[0] - 255) <= 8 and abs(px[1] - 128) <= 8 and abs(px[2] - 0) <= 8,
          f"实际 {px}")
    check("alpha 精确", px[3] == 200)
    check("数据不够时返回 None", W.decode_bc(b"\0" * 8, 8, 8, "bc3") is None)


def t_png():
    print("\n[6] 无依赖写 PNG")
    w, h = 3, 2
    data = bytes(range(w * h * 3))
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "t.png")
        W.write_png(p, w, h, data, "RGB")
        blob = open(p, "rb").read()
    check("PNG 签名", blob.startswith(b"\x89PNG\r\n\x1a\n"))
    check("IHDR 尺寸对", struct.unpack_from(">II", blob, 16) == (w, h))
    idat = blob[blob.index(b"IDAT") + 4: blob.rindex(b"IEND") - 4]
    rows = zlib.decompress(idat)
    want = b"".join(b"\x00" + data[y * w * 3:(y + 1) * w * 3] for y in range(h))
    check("像素数据往返一致", rows == want)


def t_e2e():
    print("\n[7] 端到端解包")
    fake_mp4 = struct.pack(">I", 24) + b"ftypmp42" + b"\0" * 200
    rgba = bytes((i * 7) % 256 for i in range(4 * 4 * 4))
    tex_pix = build_tex(0, 4, 4, [(1, lz4_literal(rgba), len(rgba))])
    tex_vid = build_tex(0, 4000, 2300, [(0, fake_mp4, 0)])
    pkg = build_pkg("PKGV0023", [
        ("materials/正片.tex", tex_vid),
        ("materials/贴图.tex", tex_pix),
        ("scene.json", b'{"ok":true}'),
    ])
    with tempfile.TemporaryDirectory() as td:
        item = os.path.join(td, "123456")
        os.makedirs(item)
        open(os.path.join(item, "scene.pkg"), "wb").write(pkg)
        open(os.path.join(item, "project.json"), "w", encoding="utf-8").write(
            '{"type":"scene","title":"自检"}')
        out = os.path.join(td, "out")
        vids = W.do_extract(item, W.load_project(item), out)
        got_mp4 = os.path.join(out, "123456", "正片.mp4")
        got_png = os.path.join(out, "123456", "贴图.png")
        check("视频被识别并落盘", os.path.isfile(got_mp4))
        check("视频字节完全一致", os.path.isfile(got_mp4) and open(got_mp4, "rb").read() == fake_mp4)
        check("LZ4 贴图解成了 PNG", os.path.isfile(got_png))
        check("原始文件都留了", os.path.isfile(os.path.join(out, "123456", "原始解包", "scene.json")))
        check("返回值里有视频", got_mp4 in vids)


def main():
    print(f"we_extract.py {W.__version__} 自检")
    for fn in (t_lz4, t_pkg, t_tex, t_formats, t_bc3, t_png, t_e2e):
        fn()
    print()
    if FAILED:
        print(f"✗ {len(FAILED)} 项没过：" + "、".join(FAILED))
        sys.exit(1)
    print("✓ 全部通过")


if __name__ == "__main__":
    main()
