#!/usr/bin/env python3
"""スライドの強調表示と表の罫線を機械的に取り出す。解説を書くときの補助。

    python3 build/inspect.py --council hoken --meeting 215 --document sanko --page 76 --emphasis
    python3 build/inspect.py --council hoken --meeting 215 --document sanko --page 77 --grid
    python3 build/inspect.py ... --page 77 --grid --band 430 618

なぜ要るか
  --emphasis  スライド上の太字・色は「事務局がどこを読ませたいか」の直接の証拠であり、
              解説の §6 で必ず拾うことになっている（SPEC §2.1）。目視だと見落とすので
              PDFのフォント指定から機械的に取り出す。
  --grid      表のセル結合はテキスト層からは分からない。ページをグレースケールに描画して
              縦方向に連続する暗画素の列（＝罫線）を検出し、どの列が結合されているかを出す。
              結合を読み違えると、ある時期の負担割合を隣の時期のものとして転記してしまう。

必要な外部コマンド: pdftohtml, pdftoppm
"""
from __future__ import annotations

import argparse
import html
import re
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "source"
GRID_DPI = 100
DARK = 128            # これより暗い画素を線とみなす
RULE_RATIO = 0.92     # 帯の高さのうちこの割合以上が暗ければ罫線とみなす


def run(cmd: list[str]) -> str:
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout


def spans(pdf: Path, page: int) -> list[tuple[int, int, int, str, bool]]:
    """(top, left, width, text, is_bold) を返す。"""
    xml = run(["pdftohtml", "-xml", "-f", str(page), "-l", str(page), "-i", "-stdout", str(pdf)])
    out = []
    for m in re.finditer(r'<text top="(\d+)" left="(\d+)" width="(\d+)"[^>]*>(.*?)</text>',
                         xml, re.S):
        inner = m.group(4)
        bold = "<b>" in inner
        text = html.unescape(re.sub(r"<[^>]+>", "", inner)).strip()
        if text:
            out.append((int(m.group(1)), int(m.group(2)), int(m.group(3)), text, bold))
    return out


def show_emphasis(pdf: Path, page: int, show_all: bool) -> None:
    items = spans(pdf, page)
    bolds = [i for i in items if i[4]]
    print(f"太字スパン: {len(bolds)}件 / 全{len(items)}件")
    for top, left, width, text, _ in bolds:
        print(f"  x={left:5}..{left+width:<5} y={top:<5} {text}")
    if not bolds:
        print("  （このページには太字が1か所もない）")
    if show_all:
        print("\n全スパン（y, x順）:")
        for top, left, width, text, bold in sorted(items):
            print(f"  {'B' if bold else ' '} x={left:5}..{left+width:<5} y={top:<5} {text}")


def read_pgm(path: Path) -> tuple[int, int, bytes]:
    data = path.read_bytes()
    m = re.match(rb"P5\s+(\d+)\s+(\d+)\s+(\d+)\s", data)
    if not m:
        raise RuntimeError(f"PGMとして読めない: {path}")
    return int(m.group(1)), int(m.group(2)), data[m.end():]


def show_grid(pdf: Path, page: int, band: tuple[int, int] | None) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        stem = Path(tmp) / "g"
        run(["pdftoppm", "-gray", "-r", str(GRID_DPI), "-f", str(page), "-l", str(page),
             str(pdf), str(stem)])
        pgm = sorted(Path(tmp).glob("g-*.pgm"))[0]
        w, h, px = read_pgm(pgm)

    print(f"描画サイズ {w}x{h} （{GRID_DPI}dpi）")

    def vertical_rules(y0: int, y1: int) -> list[int]:
        found = []
        for x in range(w):
            dark = sum(1 for y in range(y0, y1) if px[y * w + x] < DARK)
            if dark > RULE_RATIO * (y1 - y0):
                found.append(x)
        return collapse(found)

    def horizontal_rules(x0: int, x1: int) -> list[int]:
        found = []
        for y in range(h):
            dark = sum(1 for x in range(x0, x1) if px[y * w + x] < DARK)
            if dark > RULE_RATIO * (x1 - x0):
                found.append(y)
        return collapse(found)

    def collapse(vals: list[int]) -> list[int]:
        groups: list[list[int]] = []
        for v in vals:
            if groups and v - groups[-1][-1] <= 2:
                groups[-1].append(v)
            else:
                groups.append([v])
        return [round(sum(g) / len(g)) for g in groups]

    hr = horizontal_rules(int(w * 0.25), int(w * 0.65))
    print(f"横罫（x {int(w*0.25)}〜{int(w*0.65)} を貫くもの）: y = {hr}")
    if band:
        y0, y1 = band
        print(f"縦罫（y {y0}〜{y1}）: x = {vertical_rules(y0, y1)}")
    else:
        if len(hr) < 2:
            print("横罫が2本未満。--band で帯を指定して縦罫を調べること")
            return
        print("各帯の縦罫（横罫で区切られた帯ごと。帯をまたぐ罫線がない＝セルが結合している）:")
        for a, b in zip(hr, hr[1:]):
            if b - a < 8:
                continue
            print(f"  y {a:4}〜{b:4}: x = {vertical_rules(a + 3, b - 3)}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--council", required=True)
    ap.add_argument("--meeting", required=True)
    ap.add_argument("--document", default="sanko")
    ap.add_argument("--page", type=int, required=True, help="PDFページ番号")
    ap.add_argument("--emphasis", action="store_true", help="太字スパンを出す")
    ap.add_argument("--all-spans", action="store_true", help="太字以外のスパンも出す")
    ap.add_argument("--grid", action="store_true", help="表の罫線を検出する")
    ap.add_argument("--band", nargs=2, type=int, metavar=("Y0", "Y1"),
                    help="縦罫を調べる帯（100dpiでのy座標）")
    args = ap.parse_args(argv)

    pdf = SOURCE / args.council / str(args.meeting) / f"{args.document}.pdf"
    if not pdf.exists():
        print(f"原典がない: {pdf}\n先に build/fetch.py を走らせること")
        return 1

    print(f"{pdf.relative_to(ROOT)}  p{args.page}")
    print("=" * 60)
    if args.emphasis or args.all_spans or not args.grid:
        show_emphasis(pdf, args.page, args.all_spans)
    if args.grid:
        print()
        show_grid(pdf, args.page, tuple(args.band) if args.band else None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
