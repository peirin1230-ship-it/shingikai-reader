#!/usr/bin/env python3
"""解説Markdownと横断KBを検証する（SPEC §4-a）。

    python3 build/validate.py --all
    python3 build/validate.py --all --check-urls      # 外部URLの死活も見る
    python3 build/validate.py --council hoken --meeting 215

検査するもの
  1. フロントマターのスキーマ（必須キー・型・語彙）
  2. _meta.yaml との突合（pdf_page / printed_page / title / chapter / slide_type / 各フラグ）
  3. 本文の節構成（テンプレートの9節がそろっているか）と一文要約の長さ
  4. 解説中の数値が、そのページの抽出テキストに現れる数値集合に含まれるか
     （text_extractable: false のページはこの検査を飛ばし、全件を人のレビューに回す）
  5. 参照キーの解決（supports_topics → kb/topics.yaml、terms → kb/glossary、
     関連スライドのリンク、相対リンクの実在）
  6. --check-urls を付けたときは外部URLの死活

ERROR があれば終了コード1。WARN は0のまま返す（CIのマージ条件はERRORのみ）。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
COUNCILS = ROOT / "councils"
KB = ROOT / "kb"
WORK = ROOT / "work"

REQUIRED_KEYS = ["id", "council", "meeting", "meeting_date", "document", "pdf_page",
                 "printed_page", "title", "chapter", "slide_type", "themes", "terms",
                 "origin", "data_source", "supports_topics", "text_extractable",
                 "third_party_figure", "confidence", "review_status", "number_verified"]
SLIDE_TYPES = {"現状データ", "制度概要", "国際比較", "経緯整理", "chapter", "cover", "toc",
               "論点提示", "対応案"}
CONFIDENCE = {"high", "medium", "low"}
REVIEW_STATUS = {"draft", "reviewed", "verified"}
SECTIONS = ["1. 一文要約", "2. 書いてあること", "3. このスライドの役割", "4. 背景",
            "5. 用語", "6. この事実が支える論点", "7. 数字の読み方の注意",
            "8. 関連スライド", "9. 出典"]
SUMMARY_LIMIT = 40

# 数値突合から除くもの。転記した数値ではないため
NUMBER_SKIP_PATTERNS = [
    r"https?://\S+",                 # URL
    r"\[[^\]]*\]\([^)]*\)",          # Markdownリンク
    r"`[^`]*`",                      # インラインコード
    r"PDF\s*\d+\s*ページ",            # 他ページへの参照
    r"\bp\d{2,3}\b",                 # p077 のようなスライド参照
    r"§\s*[\d\-.]+",                 # SPECの節番号
    r"\d{4}-\d{2}(?:-\d{2})?",       # ISO日付・年月
    r"第\s*\d+\s*回",                # 審議会の回次
    r"令和\d+年法律第\d+号",           # 法律番号
    r"[\u2460-\u2473]",             # ①②… NFKC正規化で 1 2 になり、2①② が 212 に化ける
    r"\bdpi\b|\bpx\b",
]


class Report:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warns: list[str] = []
        self.checked = 0

    def error(self, where: str, msg: str) -> None:
        self.errors.append(f"ERROR {where}: {msg}")

    def warn(self, where: str, msg: str) -> None:
        self.warns.append(f"WARN  {where}: {msg}")


def load_yaml(path: Path):
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def split_frontmatter(text: str) -> tuple[dict | None, str]:
    if not text.startswith("---\n"):
        return None, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return None, text
    return yaml.safe_load(text[4:end]), text[end + 5:]


def visible_len(s: str) -> int:
    """全角・半角を区別せず素直に文字数で数える。"""
    return len(unicodedata.normalize("NFC", s.strip()))


def numbers_in(text: str) -> set[str]:
    """比較用に正規化した数値トークンの集合。"""
    text = unicodedata.normalize("NFKC", text)
    text = text.replace(",", "").replace("，", "")
    out = set()
    for m in re.finditer(r"\d+(?:\.\d+)?", text):
        tok = m.group(0)
        if "." in tok:
            tok = tok.rstrip("0").rstrip(".")
        out.add(tok or "0")
    return out


def body_numbers(body: str) -> set[str]:
    scrubbed = body
    for pat in NUMBER_SKIP_PATTERNS:
        scrubbed = re.sub(pat, " ", scrubbed)
    return numbers_in(scrubbed)


def check_slide(path: Path, meta_by_page: dict, topics: set[str], glossary: set[str],
                pages_json: dict, rep: Report) -> None:
    where = str(path.relative_to(ROOT))
    fm, body = split_frontmatter(path.read_text(encoding="utf-8"))
    if fm is None:
        rep.error(where, "フロントマターがない、または閉じていない")
        return
    rep.checked += 1

    # ---- 1. スキーマ ----
    for key in REQUIRED_KEYS:
        if key not in fm:
            rep.error(where, f"必須キーがない: {key}")
    if fm.get("slide_type") not in SLIDE_TYPES:
        rep.error(where, f"slide_type が語彙にない: {fm.get('slide_type')}")
    if fm.get("confidence") not in CONFIDENCE:
        rep.error(where, f"confidence が語彙にない: {fm.get('confidence')}")
    if fm.get("review_status") not in REVIEW_STATUS:
        rep.error(where, f"review_status が語彙にない: {fm.get('review_status')}")
    for key in ("text_extractable", "third_party_figure", "number_verified"):
        if not isinstance(fm.get(key), bool):
            rep.error(where, f"{key} は true/false で書くこと: {fm.get(key)!r}")

    expected_id = f"{fm.get('council')}-{fm.get('meeting')}-sanko-p{fm.get('pdf_page'):03d}" \
        if isinstance(fm.get("pdf_page"), int) else None
    if expected_id and fm.get("id") != expected_id:
        rep.warn(where, f"id が命名規則と違う: {fm.get('id')} （想定 {expected_id}）")
    if isinstance(fm.get("pdf_page"), int) and path.stem != f"p{fm['pdf_page']:03d}":
        rep.error(where, f"ファイル名と pdf_page が食い違う: {path.stem}")

    # ---- 2. _meta.yaml との突合 ----
    slide = meta_by_page.get(fm.get("pdf_page"))
    if slide is None:
        rep.error(where, f"_meta.yaml に pdf_page {fm.get('pdf_page')} の項目がない")
    else:
        for fm_key, meta_key in [("printed_page", "printed_page"), ("title", "title"),
                                 ("slide_type", "type")]:
            if fm.get(fm_key) != slide.get(meta_key):
                rep.error(where, f"_meta.yaml と食い違う {fm_key}: "
                                 f"{fm.get(fm_key)!r} vs {slide.get(meta_key)!r}")
        if fm.get("text_extractable") != bool(slide.get("text_ok", True)):
            rep.error(where, f"text_extractable が _meta.yaml の text_ok と食い違う")
        if fm.get("third_party_figure") != bool(slide.get("third_party_figure", False)):
            rep.error(where, "third_party_figure が _meta.yaml と食い違う")
        # themes / terms / supports_topics は解説を書く過程で増えてよい（減ってはいけない）
        for key in ("themes", "terms", "supports_topics"):
            missing = set(slide.get(key) or []) - set(fm.get(key) or [])
            if missing:
                rep.warn(where, f"_meta.yaml にあるが解説側にない {key}: {sorted(missing)}")

    # ---- 3. 節構成と一文要約 ----
    heads = re.findall(r"^##\s+(.+)$", body, re.M)
    for name in SECTIONS:
        if not any(h.strip().startswith(name) for h in heads):
            rep.error(where, f"節がない: ## {name}")
    m = re.search(r"^##\s*1\.\s*一文要約\s*$(.*?)^##", body, re.M | re.S)
    if m:
        summary = " ".join(l.strip() for l in m.group(1).strip().splitlines() if l.strip())
        if not summary:
            rep.error(where, "一文要約が空")
        elif visible_len(summary) > SUMMARY_LIMIT:
            rep.error(where, f"一文要約が{SUMMARY_LIMIT}字を超えている（{visible_len(summary)}字）: {summary}")

    # ---- 4. 数値の突合 ----
    page_rec = pages_json.get(fm.get("pdf_page"))
    if not fm.get("text_extractable"):
        rep.warn(where, "text_extractable: false。数値の機械突合は行わない。人が画像と照合すること")
        if fm.get("number_verified"):
            rep.error(where, "text_extractable: false なのに number_verified: true になっている")
        if "【要確認】" not in body:
            rep.warn(where, "text_extractable: false のページだが本文に【要確認】がない")
    elif page_rec is None:
        rep.warn(where, "抽出テキストがない（build/extract.py を先に走らせる）")
    else:
        txt = WORK / fm["council"] / str(fm["meeting"]) / "sanko" / f"p{fm['pdf_page']:03d}.txt"
        if txt.exists():
            source_nums = numbers_in(txt.read_text(encoding="utf-8", errors="replace"))
            unknown = sorted(body_numbers(body) - source_nums,
                             key=lambda x: (len(x), x), reverse=True)
            # 1〜2桁は節番号・箇条書き番号と紛れるので報告しない
            unknown = [u for u in unknown if len(u.replace(".", "")) >= 3]
            if unknown:
                rep.warn(where, f"原典の抽出テキストにない数値 {len(unknown)}件: "
                                f"{unknown[:12]}{' …' if len(unknown) > 12 else ''}")

    # ---- 5. 参照の解決 ----
    for topic in fm.get("supports_topics") or []:
        if topic not in topics:
            rep.error(where, f"supports_topics が kb/topics.yaml で解決できない: {topic}")
    for term in fm.get("terms") or []:
        if term not in glossary:
            rep.warn(where, f"terms が kb/glossary にない（未作成）: {term}")
    for link in re.findall(r"\]\((?!https?:)([^)#]+)", body):
        target = (path.parent / link).resolve()
        if not target.exists():
            rep.error(where, f"リンク先がない: {link}")


def check_kb(rep: Report) -> tuple[set[str], set[str]]:
    topics_path = KB / "topics.yaml"
    topics_raw = load_yaml(topics_path) or []
    titles, keys = set(), set()
    for t in topics_raw:
        if "key" not in t or "title" not in t:
            rep.error("kb/topics.yaml", f"key と title は必須: {t}")
            continue
        if t["title"] in titles:
            rep.error("kb/topics.yaml", f"title が重複: {t['title']}")
        if t["key"] in keys:
            rep.error("kb/topics.yaml", f"key が重複: {t['key']}")
        titles.add(t["title"])
        keys.add(t["key"])
        if t.get("evidence_slides"):
            rep.error("kb/topics.yaml",
                      f"{t['key']}: evidence_slides は build.py が逆生成する。手書きしない")

    glossary = set()
    for path in sorted((KB / "glossary").glob("*.md")):
        fm, _ = split_frontmatter(path.read_text(encoding="utf-8"))
        if fm is None:
            rep.error(str(path.relative_to(ROOT)), "フロントマターがない")
            continue
        for key in ("term", "key"):
            if key not in fm:
                rep.error(str(path.relative_to(ROOT)), f"必須キーがない: {key}")
        if fm.get("key") and path.stem != fm["key"]:
            rep.error(str(path.relative_to(ROOT)), f"ファイル名と key が違う: {fm['key']}")
        if fm.get("term"):
            glossary.add(fm["term"])
            glossary.update(fm.get("aliases") or [])
        for topic in fm.get("topics") or []:
            if topic not in titles:
                rep.error(str(path.relative_to(ROOT)), f"topics が解決できない: {topic}")

    timeline = load_yaml(KB / "timeline.yaml") or {}
    seen = set()
    for entry in timeline.get("entries", []):
        if entry["id"] in seen:
            rep.error("kb/timeline.yaml", f"id が重複: {entry['id']}")
        seen.add(entry["id"])
        for topic in entry.get("topics") or []:
            if topic not in titles:
                rep.error("kb/timeline.yaml", f"{entry['id']}: topics が解決できない: {topic}")

    for doc in load_yaml(KB / "documents.yaml") or []:
        if not doc.get("url"):
            rep.error("kb/documents.yaml", f"{doc.get('key')}: url は必須")
        for topic in doc.get("topics") or []:
            if topic not in titles:
                rep.error("kb/documents.yaml", f"{doc.get('key')}: topics が解決できない: {topic}")

    return titles, glossary


def collect_urls() -> set[str]:
    urls = set()
    for path in list(ROOT.glob("kb/**/*")) + list(ROOT.glob("councils/**/*")) + [ROOT / "README.md"]:
        if path.is_file() and path.suffix in {".md", ".yaml"}:
            text = path.read_text(encoding="utf-8", errors="replace")
            urls.update(re.findall(r"https?://[^\s<>\"'\)\]]+", text))
    return {u.rstrip(".,;)") for u in urls}


def check_urls(rep: Report) -> None:
    for url in sorted(collect_urls()):
        req = urllib.request.Request(url, method="HEAD",
                                     headers={"User-Agent": "shingikai-reader/0.1"})
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                if resp.status >= 400:
                    rep.error("urls", f"HTTP {resp.status}: {url}")
                else:
                    print(f"  {resp.status}  {url}")
        except Exception as exc:  # noqa: BLE001
            rep.warn("urls", f"到達できない（{exc}）: {url}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--council")
    ap.add_argument("--meeting")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--check-urls", action="store_true")
    args = ap.parse_args(argv)
    if not args.all and not args.council:
        ap.error("--council か --all のどちらかを指定すること")

    rep = Report()
    topics, glossary = check_kb(rep)

    pattern = f"{args.council or '*'}/{args.meeting or '*'}/_meta.yaml"
    for meta_path in sorted(COUNCILS.glob(pattern)):
        meta = load_yaml(meta_path)
        meta_by_page = {s["pdf_page"]: s for s in meta.get("slides", [])}
        council, meeting = meta["council"], str(meta["meeting"])

        pages_json: dict = {}
        pj = WORK / council / meeting / "sanko" / "pages.json"
        if pj.exists():
            pages_json = {p["pdf_page"]: p for p in json.loads(pj.read_text(encoding="utf-8"))["pages"]}

        for slide_path in sorted(meta_path.parent.glob("*/p*.md")):
            check_slide(slide_path, meta_by_page, topics, glossary, pages_json, rep)

    if args.check_urls:
        print("URLの死活:")
        check_urls(rep)

    for line in rep.warns:
        print(line)
    for line in rep.errors:
        print(line)
    print(f"\nスライド {rep.checked}枚を検証   ERROR {len(rep.errors)} / WARN {len(rep.warns)}")
    return 1 if rep.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
