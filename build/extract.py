#!/usr/bin/env python3
"""原典PDFを、スライド画像（images/）と抽出テキスト（work/）に分解する。

    python3 build/extract.py --council hoken --meeting 215 --document sanko
    python3 build/extract.py --all

生成するもの（すべて .gitignore の対象）:

    images/{council}/{meeting}/{doc}/pNNN.webp       1600px 幅
    images/{council}/{meeting}/{doc}/pNNN.thumb.webp  320px 幅
    work/{council}/{meeting}/{doc}/pNNN.txt           pdftotext -layout
    work/{council}/{meeting}/{doc}/pNNN.bold.txt      太字スパン（SPEC §2.1 §6用）
    work/{council}/{meeting}/{doc}/pNNN.ocr.txt       OCR結果（--ocr のとき）
    work/{council}/{meeting}/{doc}/pages.json         ページごとの抽出文字数など

抽出文字数（空白を除く）が100字未満のページには text_extractable: false を立てる。
_meta.yaml に text_ok があれば突き合わせ、食い違いを警告する。
章扉・表紙・目次はもともと文字数が少ないので、この判定からは除く。

★OCRは補助であって、原典のテキスト層の代わりにはならない。
  図表が画像で貼られたページのテキストを読むための手がかりとして置くだけで、
  数値の突合には使わない（validate.py はOCR結果を参照しない）。
  実測した精度の限界は docs/pipeline-notes.md §8 を見ること。

必要な外部コマンド: pdftoppm, pdftotext, pdfinfo, cwebp, pdftohtml
                  OCRを使うときは tesseract（tesseract-ocr-jpn も要る）
"""
from __future__ import annotations

import argparse
import html
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
COUNCILS = ROOT / "councils"
SOURCE = ROOT / "source"
IMAGES = ROOT / "images"
WORK = ROOT / "work"

TEXT_THRESHOLD = 100          # 空白を除く抽出文字数がこれ未満なら text_extractable: false
LOW_TEXT_EXEMPT = {"cover", "toc", "chapter"}   # もともと文字が少ない種別

# ★文字数の数え方について（docs/pipeline-notes.md 参照）
#   SPEC §0.3 の表に載っている抽出文字数は、実測するとバイト数だった
#   （日本語1文字=UTF-8で3バイト）。文字数で数え直すと閾値100を下回るページが
#   7枚から14枚に増える。どちらが正しいかは人が決めることなので、
#   両方を pages.json に記録し、_meta.yaml の text_ok を正とする。
RENDER_DPI = 200
WIDE_PX = 1600
THUMB_PX = 320
WEBP_Q = 80

# OCRの設定。400dpi・psm 4（段の中で行の大きさが変わるレイアウト）が
# 手元の比較でいちばん妥当だった（docs/pipeline-notes.md §8）
OCR_DPI = 400
OCR_LANG = "jpn"
OCR_PSM = "4"

REQUIRED = ["pdftoppm", "pdftotext", "pdfinfo", "cwebp", "pdftohtml"]


def check_tools(need_ocr: bool) -> None:
    missing = [c for c in REQUIRED if shutil.which(c) is None]
    if missing:
        print(f"必要なコマンドがない: {', '.join(missing)}\n"
              f"  Debian/Ubuntu: sudo apt-get install -y poppler-utils webp\n"
              f"  macOS:         brew install poppler webp", file=sys.stderr)
        raise SystemExit(1)
    if need_ocr and shutil.which("tesseract") is None:
        print("OCRを指定したが tesseract がない\n"
              "  Debian/Ubuntu: sudo apt-get install -y tesseract-ocr tesseract-ocr-jpn\n"
              "  macOS:         brew install tesseract tesseract-lang\n"
              "  OCRなしで進めるなら --ocr off", file=sys.stderr)
        raise SystemExit(1)


def run(cmd: list[str]) -> str:
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout


def page_count(pdf: Path) -> int:
    for line in run(["pdfinfo", str(pdf)]).splitlines():
        if line.startswith("Pages:"):
            return int(line.split(":", 1)[1])
    raise RuntimeError(f"ページ数を取得できない: {pdf}")


def bold_spans(pdf: Path, page: int) -> list[str]:
    """そのページで太字が当たっているテキストを、出現順に返す。

    強調表示は「事務局がどこを読ませたいか」の直接の証拠なので機械的に拾う（SPEC §2.1）。
    """
    xml = run(["pdftohtml", "-xml", "-f", str(page), "-l", str(page), "-i", "-stdout", str(pdf)])
    spans: list[str] = []
    for text_el in re.finditer(r"<text[^>]*>(.*?)</text>", xml, re.S):
        for bold in re.finditer(r"<b>(.*?)</b>", text_el.group(1), re.S):
            plain = html.unescape(re.sub(r"<[^>]+>", "", bold.group(1))).strip()
            if plain:
                spans.append(plain)
    return spans


def ocr_page(pdf: Path, page: int, tmp: str) -> str:
    """1ページをOCRして文字列を返す。読み取り精度は保証しない。"""
    stem = Path(tmp) / f"ocr{page:03d}"
    run(["pdftoppm", "-r", str(OCR_DPI), "-gray", "-png", "-f", str(page), "-l", str(page),
         str(pdf), str(stem)])
    pngs = sorted(Path(tmp).glob(f"ocr{page:03d}-*.png"))
    if not pngs:
        return ""
    png = pngs[0]
    out = Path(tmp) / f"ocr{page:03d}"
    run(["tesseract", str(png), str(out), "-l", OCR_LANG, "--psm", OCR_PSM])
    png.unlink()
    result = out.with_suffix(".txt")
    text = result.read_text(encoding="utf-8", errors="replace") if result.exists() else ""
    if result.exists():
        result.unlink()
    return text


def load_meta(council: str, meeting: str) -> dict:
    path = COUNCILS / council / str(meeting) / "_meta.yaml"
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def extract_document(council: str, meeting: str, key: str, meta: dict, force: bool,
                     ocr_mode: str) -> list[dict]:
    pdf = SOURCE / council / str(meeting) / f"{key}.pdf"
    if not pdf.exists():
        print(f"  {council}/{meeting}/{key}: 原典がない（先に fetch.py を走らせる）→ {pdf}")
        return []

    img_dir = IMAGES / council / str(meeting) / key
    txt_dir = WORK / council / str(meeting) / key
    img_dir.mkdir(parents=True, exist_ok=True)
    txt_dir.mkdir(parents=True, exist_ok=True)

    total = page_count(pdf)
    # _meta.yaml のスライド索引は、その索引が対象にしている資料のものでしかない。
    # 別の資料に流用すると、ページ番号だけが一致した無関係なスライド情報が付いてしまう
    meta_key = (meta.get("document") or {}).get("key")
    has_index = bool(meta) and meta_key == key
    by_page = {s["pdf_page"]: s for s in meta.get("slides", [])} if has_index else {}

    print(f"  {council}/{meeting}/{key}: {total}ページ")
    if meta and not has_index:
        print(f"    （_meta.yaml の索引は {meta_key} のもの。この資料には使わない）")
    pages: list[dict] = []
    with tempfile.TemporaryDirectory() as tmp:
        for page in range(1, total + 1):
            tag = f"p{page:03d}"
            txt_path = txt_dir / f"{tag}.txt"
            wide = img_dir / f"{tag}.webp"
            thumb = img_dir / f"{tag}.thumb.webp"

            if force or not txt_path.exists():
                run(["pdftotext", "-layout", "-f", str(page), "-l", str(page),
                     str(pdf), str(txt_path)])
            if force or not (wide.exists() and thumb.exists()):
                stem = Path(tmp) / tag
                run(["pdftoppm", "-r", str(RENDER_DPI), "-png", "-f", str(page), "-l", str(page),
                     str(pdf), str(stem)])
                pngs = sorted(Path(tmp).glob(f"{tag}-*.png"))
                if not pngs:
                    raise RuntimeError(f"描画に失敗した: {pdf} p{page}")
                png = pngs[0]
                run(["cwebp", "-quiet", "-q", str(WEBP_Q), "-resize", str(WIDE_PX), "0",
                     str(png), "-o", str(wide)])
                run(["cwebp", "-quiet", "-q", str(WEBP_Q), "-resize", str(THUMB_PX), "0",
                     str(png), "-o", str(thumb)])
                png.unlink()

            body = txt_path.read_text(encoding="utf-8", errors="replace")
            stripped = re.sub(r"\s", "", body)
            chars = len(stripped)
            nbytes = len(stripped.encode("utf-8"))

            spans = bold_spans(pdf, page)
            (txt_dir / f"{tag}.bold.txt").write_text("\n".join(spans) + ("\n" if spans else ""),
                                                     encoding="utf-8")

            slide = by_page.get(page, {})
            slide_type = slide.get("type")
            exempt = slide_type in LOW_TEXT_EXEMPT
            declared = slide.get("text_ok")

            auto_chars = True if exempt else chars >= TEXT_THRESHOLD
            auto_bytes = True if exempt else nbytes >= TEXT_THRESHOLD
            # _meta.yaml の宣言があればそれを正とする。なければ文字数基準に落とす
            effective = declared if declared is not None else auto_chars

            record = {
                "pdf_page": page,
                "printed_page": slide.get("printed_page"),
                "title": slide.get("title"),
                "type": slide_type,
                "chars": chars,
                "bytes": nbytes,
                "text_extractable": effective,
                "auto_by_chars": auto_chars,
                "auto_by_bytes": auto_bytes,
                "declared_text_ok": declared,
                "low_text_exempt": exempt,
                "bold_spans": spans,
            }

            if declared is not None and not exempt and declared != auto_chars:
                record["meta_mismatch"] = True

            # ---- OCR ----
            # 既定（missing）は「テキスト層から中身を読めないページ」を対象にする。
            # _meta.yaml の宣言と文字数基準のどちらかが false ならOCRする。
            #   宣言が false … 大型の数表が丸ごと画像のページ
            #   文字数が少ない … 題名と出典しかなく、グラフの数値が画像の中にあるページ
            # OCR結果は補助であり、text_extractable や number_verified には影響しない
            ocr_path = txt_dir / f"{tag}.ocr.txt"
            want_ocr = (ocr_mode == "all") or (
                ocr_mode == "missing" and not exempt and not (effective and auto_chars))
            if want_ocr:
                if force or not ocr_path.exists():
                    ocr_text = ocr_page(pdf, page, tmp)
                    ocr_path.write_text(ocr_text, encoding="utf-8")
                else:
                    ocr_text = ocr_path.read_text(encoding="utf-8", errors="replace")
                record["ocr"] = True
                record["ocr_chars"] = len(re.sub(r"\s", "", ocr_text))
                record["ocr_note"] = "OCRは補助。数値は原典の画像と人が照合すること"
            else:
                record["ocr"] = False

            pages.append(record)

    (txt_dir / "pages.json").write_text(
        json.dumps({"council": council, "meeting": meeting, "document": key,
                    "pages": pages}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")

    low = [p["pdf_page"] for p in pages if not p["text_extractable"]]
    only_chars = [p["pdf_page"] for p in pages if p.get("meta_mismatch")]
    ocred = [p["pdf_page"] for p in pages if p.get("ocr")]
    print(f"    画像 {total*2} 枚 / テキスト {total} ファイル")
    basis = "_meta.yaml の宣言に従う" if has_index else f"文字数 {TEXT_THRESHOLD}字未満"
    print(f"    text_extractable: false（{basis}）→ {low if low else 'なし'}")
    if only_chars:
        print(f"    ! 文字数基準（<{TEXT_THRESHOLD}字）では false になるが _meta.yaml は text_ok: true → {only_chars}")
        print(f"      図表が画像で、テキスト層に題名と出典しかないページ。人が判断すること")
        print(f"      （docs/pipeline-notes.md 参照）")
    if ocred:
        print(f"    OCR {len(ocred)}ページ → *.ocr.txt "
              f"（補助。数値は人が原典と照合すること）")
    return pages


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--council")
    ap.add_argument("--meeting")
    ap.add_argument("--document")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--force", action="store_true", help="既存の生成物を作り直す")
    ap.add_argument("--include-inactive", action="store_true",
                    help="manifest の active: false の資料も対象にする")
    ap.add_argument("--ocr", choices=["off", "missing", "all"], default="missing",
                    help="OCRの対象。off=しない / missing=テキスト層が空のページだけ（既定） / all=全ページ")
    args = ap.parse_args(argv)

    if not args.all and not args.council:
        ap.error("--council か --all のどちらかを指定すること")
    check_tools(args.ocr != "off")

    pattern = f"{args.council or '*'}/{args.meeting or '*'}/manifest.yaml"
    done = 0
    for manifest_path in sorted(COUNCILS.glob(pattern)):
        with manifest_path.open(encoding="utf-8") as fh:
            manifest = yaml.safe_load(fh)
        council, meeting = manifest["council"], str(manifest["meeting"])
        meta = load_meta(council, meeting)
        print(f"{manifest_path.relative_to(ROOT)}")
        for doc in manifest.get("documents", []):
            if args.document and doc["key"] != args.document:
                continue
            if not args.document and not args.include_inactive and not doc.get("active", True):
                continue
            if extract_document(council, meeting, doc["key"], meta, args.force, args.ocr):
                done += 1

    if not done:
        print("分解できた資料がない。fetch.py で原典を取得したか確認すること", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
