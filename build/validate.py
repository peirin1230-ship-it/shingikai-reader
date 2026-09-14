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
  7. 「第NNN回」と同じ行に書かれた日付が councils/{会議体}/meetings.yaml の開催日と合っているか
     （フロントマターの meeting / meeting_date、年表 title の回次も同じ表で検査する。
      原典の誤記をそのまま引用する行は【原典ママ】を付けて除外する）
  8. --check-meetings を付けたときは、meetings.yaml を会議体の資料一覧ページ（index_url）と再照合
  9. kb/documents.yaml の PDF に sha256 が付いているか

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
# 本文テンプレートの9節（SPEC §2.1）。中身のあるスライドに要求する
SECTIONS = ["1. 一文要約", "2. 書いてあること", "3. このスライドの役割", "4. 背景",
            "5. 用語", "6. この事実が支える論点", "7. 数字の読み方の注意",
            "8. 関連スライド", "9. 出典"]
# 章扉・表紙・目次は「章の狙いと収録スライドの一覧のみ」でよい（SPEC §2.1 の表）
SECTIONS_LIGHT = ["1. 一文要約", "9. 出典"]
LIGHT_TYPES = {"chapter", "cover", "toc"}
SUMMARY_LIMIT = 40

# ---- 回次と開催日の突合（councils/{key}/meetings.yaml）----
# 「第221回国会」は会議体の回次ではないので除く
MEETING_REF = re.compile(r"第\s*(\d{1,4})\s*回(?!国会)")
ERA_BASE = {"令和": 2018, "平成": 1988, "昭和": 1925}
DATE_RES = [
    re.compile(r"(?P<y>\d{4})-(?P<m>\d{2})-(?P<d>\d{2})"),
    re.compile(r"(?P<y>\d{4})年\s*(?P<m>\d{1,2})月\s*(?P<d>\d{1,2})日"),
    re.compile(r"(?P<era>令和|平成|昭和)\s*(?P<ey>\d{1,2})年\s*(?P<m>\d{1,2})月\s*(?P<d>\d{1,2})日"),
    re.compile(r"(?<![\d年])(?P<m>\d{1,2})月\s*(?P<d>\d{1,2})日"),   # 年のない「9月3日」
]
# 原典の誤記をそのまま引用する行に付ける印。この印がある行は突合しない
SOURCE_ASIS = "【原典ママ】"
# 日付の直後にこれらが続くときは「その日に何かをした」日付であって開催日ではない
DATE_NOT_MEETING = ("時点", "現在", "閲覧", "取得", "確認", "実施", "公布", "施行", "閣議決定")
MEETINGS: dict[str, dict] = {}

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
    r"注\s*\d+",                     # 他文書の脚注番号（骨太の注126 など）
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


def load_meetings() -> dict[str, dict]:
    """councils/{key}/meetings.yaml を読み、{会議体: {aliases, index_url, meetings: {回次: 'YYYY-MM-DD'}}} を返す。"""
    regs: dict[str, dict] = {}
    for path in sorted(COUNCILS.glob("*/meetings.yaml")):
        data = load_yaml(path) or {}
        regs[path.parent.name] = {
            "aliases": [str(a) for a in (data.get("ref_aliases") or [])],
            "index_url": data.get("index_url"),
            "meetings": {int(m["number"]): str(m["date"]) for m in (data.get("meetings") or [])},
        }
    return regs


def dates_in(line: str) -> list[tuple[int, int, int | None, int, int]]:
    """行内の日付を (開始位置, 終了位置, 年 or None, 月, 日) で返す。年のない「9月3日」も拾う。
    「2026年9月14日時点」のように開催日でないことが明らかな日付は返さない。"""
    out: list[tuple[int, int, int | None, int, int]] = []
    taken: list[tuple[int, int]] = []
    for rx in DATE_RES:
        for m in rx.finditer(line):
            if any(a <= m.start() < b for a, b in taken):
                continue
            taken.append((m.start(), m.end()))
            if line[m.end():].lstrip("（(").startswith(DATE_NOT_MEETING):
                continue
            g = m.groupdict()
            if g.get("era"):
                year: int | None = ERA_BASE[g["era"]] + int(g["ey"])
            else:
                year = int(g["y"]) if g.get("y") else None
            out.append((m.start(), m.end(), year, int(g["m"]), int(g["d"])))
    return out


def check_meeting_line(where: str, raw: str, file_council: str | None, rep: Report) -> None:
    """1行の中の「第NNN回」を、いちばん近い日付と組にして meetings.yaml と突合する。"""
    if SOURCE_ASIS in raw:
        return
    line = unicodedata.normalize("NFKC", raw)
    refs = list(MEETING_REF.finditer(line))
    if not refs:
        return
    dates = dates_in(line)
    if not dates:
        return
    for m in refs:
        no = int(m.group(1))
        for key, reg in MEETINGS.items():
            # 会議体のディレクトリ内か、行に会議体名（別名）があるときだけ、その会議体として読む
            if key != file_council and not any(a in line for a in reg["aliases"]):
                continue
            expected = reg["meetings"].get(no)
            if not expected:
                continue
            # 回次と日付の「隙間」が最も小さいものを組にする（前後どちらにあってもよい）
            _, _, y, mo, d = min(dates, key=lambda t: max(0, max(t[0], m.start()) - min(t[1], m.end())))
            ey, em, ed = (int(x) for x in expected.split("-"))
            if (mo, d) != (em, ed) or (y is not None and y != ey):
                rep.error(where, f"第{no}回（{key}）の開催日は {expected} だが、行内の日付は "
                                 f"{y if y else '????'}-{mo:02d}-{d:02d}: {raw.strip()[:70]}")


def check_meeting_refs(rep: Report) -> None:
    """解説・KB・docs・README の全行を回次と日付の組で検査する。"""
    files = [p for p in list(ROOT.glob("councils/**/*")) + list(ROOT.glob("kb/**/*"))
             + list(ROOT.glob("docs/*.md")) + [ROOT / "README.md"]
             if p.is_file() and p.suffix in {".md", ".yaml"}]
    for path in sorted(files):
        rel = path.relative_to(ROOT)
        file_council = rel.parts[1] if rel.parts[0] == "councils" and len(rel.parts) > 2 else None
        for n, raw in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            check_meeting_line(f"{rel}:{n}", raw, file_council, rep)


def check_meetings_online(rep: Report) -> None:
    """index_url の表を取得し、meetings.yaml の回次と開催日を照合する（厚労省の資料一覧ページの表構造に依存）。"""
    for key, reg in MEETINGS.items():
        url = reg.get("index_url")
        if not url:
            continue
        req = urllib.request.Request(url, headers={"User-Agent": "shingikai-reader/0.1"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                page = resp.read().decode("utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001
            rep.warn("meetings", f"{key}: index_url に到達できない（{exc}）: {url}")
            continue
        found: dict[int, str] = {}
        for m in re.finditer(r"第(\d+)回</td>\s*<td[^>]*>(\d{4})年(\d{1,2})月(\d{1,2})日", page):
            found[int(m.group(1))] = f"{m.group(2)}-{int(m.group(3)):02d}-{int(m.group(4)):02d}"
        if not found:
            rep.warn("meetings", f"{key}: index_url から回次と開催日を読み取れなかった（ページ構造が変わった可能性）: {url}")
            continue
        for no, date in sorted(found.items()):
            if no not in reg["meetings"]:
                rep.warn("meetings", f"{key}: 第{no}回（{date}）が meetings.yaml にない。追記すること")
            elif reg["meetings"][no] != date:
                rep.error("meetings", f"{key}: 第{no}回の開催日が食い違う meetings.yaml={reg['meetings'][no]} / index_url={date}")
        print(f"  {key}: index_url から {len(found)} 回分を読み取り、meetings.yaml と照合した")


def check_slide(path: Path, meta_by_page: dict, topics: set[str], glossary: set[str],
                pages_json: dict, rep: Report, doc_key: str = "sanko",
                indexes: dict | None = None) -> None:
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

    expected_id = f"{fm.get('council')}-{fm.get('meeting')}-{doc_key}-p{fm.get('pdf_page'):03d}" \
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

    # ---- 2b. 回次と開催日（councils/{key}/meetings.yaml）----
    reg = MEETINGS.get(str(fm.get("council")), {}).get("meetings", {})
    if isinstance(fm.get("meeting"), int) and fm["meeting"] in reg \
            and str(fm.get("meeting_date")) != reg[fm["meeting"]]:
        rep.error(where, f"meeting_date {fm.get('meeting_date')} が meetings.yaml の"
                         f"第{fm['meeting']}回の開催日 {reg[fm['meeting']]} と食い違う")

    # ---- 3. 節構成と一文要約 ----
    heads = re.findall(r"^##\s+(.+)$", body, re.M)
    required = SECTIONS_LIGHT if fm.get("slide_type") in LIGHT_TYPES else SECTIONS
    for name in required:
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
        txt = WORK / fm["council"] / str(fm["meeting"]) / doc_key / f"p{fm['pdf_page']:03d}.txt"
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
        if target.exists():
            continue
        # 同じ資料の別スライドへのリンクは、_meta.yaml に索引があれば有効とする。
        # ビューアはスライドIDで解決するので、解説がまだ書かれていなくてもリンクは切れない
        m = re.fullmatch(r"p(\d{3})\.md", link.strip())
        if m and int(m.group(1)) in meta_by_page:
            rep.warn(where, f"リンク先の解説が未作成（索引にはある）: {link}")
            continue
        # 同じ回の別資料へのリンク（../{資料}/pNNN.md）
        m2 = re.fullmatch(r"\.\./([\w\-]+)/p(\d{3})\.md", link.strip())
        if m2 and (indexes or {}).get(m2.group(1), {}).get(int(m2.group(2))):
            rep.warn(where, f"リンク先の解説が未作成（別資料の索引にはある）: {link}")
            continue
        rep.error(where, f"リンク先がない: {link}")

    # ---- 5b. リンクのラベルが指す先のタイトルと合っているか ----
    # 「[参考資料 p081 保険者の予防・健康づくりの取組](../sanko/p081.md)」のように、
    # ページ番号を取り違えるとリンクは通るのにラベルが別のスライドを指す。
    # ラベルと索引上のタイトルの文字の重なりが薄いときだけ警告する（略称は許す）。
    for label, doc, pg in re.findall(
            r"\[([^\]]{2,80}?)\]\((?:\.\./([\w\-]+)/)?p(\d{3})\.md\)", body):
        idx = (indexes or {}).get(doc or doc_key) or (meta_by_page if doc is None else None)
        rec = (idx or {}).get(int(pg))
        if not rec:
            continue
        real = str(rec.get("title") or "")
        lab = re.sub(r"^(参考資料|資料\d)?\s*p\d{3}\s*", "", label).strip()
        # ラベルが資料そのものの呼び名（「資料1」「参考資料」）のときは表紙を指すので照合しない
        if not lab or not real or lab in {"参考資料", "資料1", "資料2", "議事次第", "委員名簿"}:
            continue
        if lab in real or real in lab:
            continue
        common = len(set(lab) & set(real))
        if common / max(len(set(lab)), 1) < 0.34:
            rep.warn(where, f"リンクのラベルが索引のタイトルと合わない: "
                            f"p{pg} 「{lab}」 vs 「{real}」")


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
        # title に「第NNN回」があり日付が日単位なら、開催日と突合する
        title = unicodedata.normalize("NFKC", str(entry.get("title") or ""))
        m = MEETING_REF.search(title)
        if m and entry.get("date_precision") == "day":
            for key, reg in MEETINGS.items():
                expected = reg["meetings"].get(int(m.group(1)))
                if any(a in title for a in reg["aliases"]) and expected \
                        and str(entry.get("date")) != expected:
                    rep.error("kb/timeline.yaml", f"{entry['id']}: 第{m.group(1)}回（{key}）の開催日は "
                                                  f"{expected} だが date は {entry.get('date')}")

    for doc in load_yaml(KB / "documents.yaml") or []:
        if not doc.get("url"):
            rep.error("kb/documents.yaml", f"{doc.get('key')}: url は必須")
        elif str(doc["url"]).lower().endswith(".pdf") and not doc.get("sha256"):
            rep.warn("kb/documents.yaml", f"{doc.get('key')}: PDF なのに sha256 がない（取得して記録すること）")
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
    ap.add_argument("--check-meetings", action="store_true",
                    help="meetings.yaml を会議体の資料一覧ページ（index_url）と再照合する（ネットワークに出る）")
    args = ap.parse_args(argv)
    if not args.all and not args.council:
        ap.error("--council か --all のどちらかを指定すること")

    rep = Report()
    global MEETINGS
    MEETINGS = load_meetings()
    topics, glossary = check_kb(rep)
    check_meeting_refs(rep)

    pattern = f"{args.council or '*'}/{args.meeting or '*'}/_meta.yaml"
    for meta_path in sorted(COUNCILS.glob(pattern)):
        meta = load_yaml(meta_path)
        council, meeting = meta["council"], str(meta["meeting"])

        # _meta.yaml が主資料、_meta.{key}.yaml が同じ回の別資料の索引
        indexes: dict[str, dict] = {}
        for mp in [meta_path] + sorted(meta_path.parent.glob("_meta.*.yaml")):
            m = load_yaml(mp) or {}
            key = (m.get("document") or {}).get("key")
            if not key:
                rep.error(str(mp.relative_to(ROOT)), "document.key がない")
                continue
            indexes[key] = {s["pdf_page"]: s for s in m.get("slides", [])}

        for slide_path in sorted(meta_path.parent.glob("*/p*.md")):
            doc_key = slide_path.parent.name
            if doc_key not in indexes:
                rep.error(str(slide_path.relative_to(ROOT)),
                          f"資料 {doc_key} の索引（_meta.{doc_key}.yaml）がない")
                continue
            pages_json: dict = {}
            pj = WORK / council / meeting / doc_key / "pages.json"
            if pj.exists():
                pages_json = {p["pdf_page"]: p
                              for p in json.loads(pj.read_text(encoding="utf-8"))["pages"]}
            check_slide(slide_path, indexes[doc_key], topics, glossary,
                        pages_json, rep, doc_key, indexes)

    if args.check_urls:
        print("URLの死活:")
        check_urls(rep)
    if args.check_meetings:
        print("回次と開催日の再照合:")
        check_meetings_online(rep)

    for line in rep.warns:
        print(line)
    for line in rep.errors:
        print(line)
    print(f"\nスライド {rep.checked}枚を検証   ERROR {len(rep.errors)} / WARN {len(rep.warns)}")
    return 1 if rep.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
