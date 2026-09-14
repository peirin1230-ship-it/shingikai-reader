#!/usr/bin/env python3
"""1スライド分の解説を書くための材料をまとめて出す（SPEC §3 Phase 2 の「渡すコンテキスト」）。

    python3 build/brief.py --page 29
    python3 build/brief.py --page 29 --neighbors 2

出すもの
  - _meta.yaml の索引（章・種別・テーマ・用語・supports_topics・note・各フラグ）
  - 当該ページの抽出テキスト
  - 太字スパン（事務局がどこを読ませたいか）
  - OCR結果（テキスト層が空のページのみ）
  - 前後ページの見出し（§3「このスライドの役割」を書くため）
  - 解説の有無
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--council", default="hoken")
    ap.add_argument("--meeting", default="215")
    ap.add_argument("--document", default="sanko")
    ap.add_argument("--page", type=int, required=True)
    ap.add_argument("--neighbors", type=int, default=1, help="前後何ページの見出しを出すか")
    args = ap.parse_args(argv)

    base = ROOT / "councils" / args.council / args.meeting
    meta = yaml.safe_load((base / "_meta.yaml").read_text(encoding="utf-8"))
    by_page = {s["pdf_page"]: s for s in meta["slides"]}
    chapters = {c["key"]: c for c in meta["chapters"]}
    work = ROOT / "work" / args.council / args.meeting / args.document
    tag = f"p{args.page:03d}"

    slide = by_page.get(args.page)
    if slide is None:
        print(f"_meta.yaml に pdf_page {args.page} がない")
        return 1
    ch = chapters.get(slide.get("chapter"), {})

    print(f"══════ pdf_page {args.page} / printed {slide.get('printed_page')} ══════")
    print(f"title  : {slide.get('title')}")
    print(f"type   : {slide.get('type')}")
    print(f"chapter: {slide.get('chapter')} = {ch.get('title')} "
          f"(PDF {ch.get('pdf_pages')}, 実質{ch.get('slides')}枚)")
    for key in ("themes", "terms", "supports_topics", "origin", "data_source", "note",
                "text_ok", "third_party_figure", "number_verified", "priority"):
        if key in slide:
            print(f"{key:18}: {slide[key]}")
    md = base / args.document / f"{tag}.md"
    print(f"解説     : {'あり ' + str(md) if md.exists() else 'なし'}")

    print(f"\n──── 前後のスライド ────")
    for n in range(args.page - args.neighbors, args.page + args.neighbors + 1):
        s = by_page.get(n)
        if not s:
            continue
        mark = "▶" if n == args.page else " "
        done = "✓" if (base / args.document / f"p{n:03d}.md").exists() else " "
        print(f" {mark}{done} p{n:03d} [{s.get('type')}] {s.get('title')}")

    bold = work / f"{tag}.bold.txt"
    if bold.exists():
        spans = [l for l in bold.read_text(encoding="utf-8").splitlines() if l.strip()]
        print(f"\n──── 太字スパン（{len(spans)}件）────")
        for x in spans:
            print(f"  • {x}")
        if not spans:
            print("  （このページには太字が1か所もない）")

    txt = work / f"{tag}.txt"
    if txt.exists():
        body = txt.read_text(encoding="utf-8", errors="replace")
        print(f"\n──── 抽出テキスト ────")
        print(body.rstrip())

    ocr = work / f"{tag}.ocr.txt"
    if ocr.exists():
        print(f"\n──── OCR（★補助。数値はそのまま転記しない）────")
        print(ocr.read_text(encoding="utf-8", errors="replace").rstrip())

    pj = work / "pages.json"
    if pj.exists():
        rec = {p["pdf_page"]: p for p in json.loads(pj.read_text(encoding="utf-8"))["pages"]}
        r = rec.get(args.page, {})
        print(f"\n抽出文字数 {r.get('chars')}字 / text_extractable={r.get('text_extractable')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
