#!/usr/bin/env python3
"""解説Markdownと横断KBから、GitHub Pages に載せるサイトを生成する。

    python3 build/build.py --out dist
    python3 build/build.py --out dist --include-third-party   # 第三者図版も配信する

生成物
    dist/index.html      ビューア本体（CSS・JSを内蔵した1ファイル）
    dist/data.json       スライド・用語・年表・論点のデータ
    dist/images/...      スライド画像（third_party_figure: true は既定で配信しない）

方針
  - kb/topics.yaml の evidence_slides は、スライド側の supports_topics から逆生成する。
    二重管理を避けるため、YAMLに手書きされていたら validate.py がエラーにする。
  - third_party_figure: true のスライドは、既定では画像そのものを dist に置かない。
    フラグ1つで方針を変えられるようにするのが目的（SPEC §7.2）。
  - number_verified: false のスライドはビューアで⚠を出す。
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mdlite  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
COUNCILS = ROOT / "councils"
KB = ROOT / "kb"
IMAGES = ROOT / "images"
TEMPLATE = Path(__file__).resolve().parent / "viewer.html"

FOOTER = (
    "本サイトは、厚生労働省ウェブサイトで公開されている審議会資料をもとに、"
    "個人が解説を加えて作成したものです。厚生労働省が作成・公認したものではありません。"
)


def load_yaml(path: Path):
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def split_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}, text
    return yaml.safe_load(text[4:end]) or {}, text[end + 5:]


def rewrite_links(md: str, slide_ids: set[str]) -> str:
    """相対リンクをビューア内のハッシュURLに書き換える。"""
    def repl(m: re.Match) -> str:
        label, target = m.group(1), m.group(2)
        if target.startswith(("http://", "https://", "#")):
            return m.group(0)
        g = re.search(r"kb/glossary/([\w\-]+)\.md", target)
        if g:
            return f"[{label}](#/g/{g.group(1)})"
        s = re.search(r"(?:^|/)p(\d{3})\.md$", target)
        if s:
            for sid in slide_ids:
                if sid.endswith(f"-p{s.group(1)}"):
                    return f"[{label}](#/s/{sid})"
        return label   # 解決できない相対リンクはただの文字にする
    return re.sub(r"\[([^\]]+)\]\(([^)]+)\)", repl, md)


def collect(include_third_party: bool) -> dict:
    councils = {c["key"]: c for c in load_yaml(KB / "councils.yaml")}
    topics = load_yaml(KB / "topics.yaml") or []
    timeline = (load_yaml(KB / "timeline.yaml") or {}).get("entries", [])
    documents = load_yaml(KB / "documents.yaml") or []

    glossary = []
    for path in sorted((KB / "glossary").glob("*.md")):
        fm, body = split_frontmatter(path.read_text(encoding="utf-8"))
        glossary.append({**fm, "body_md": body})

    meetings, slides = [], []
    for meta_path in sorted(COUNCILS.glob("*/*/_meta.yaml")):
        meta = load_yaml(meta_path)
        council, meeting = meta["council"], str(meta["meeting"])
        doc = meta.get("document", {})
        meetings.append({
            "council": council, "meeting": meeting,
            "council_name": councils.get(council, {}).get("name", council),
            "date": str(meta.get("meeting_date", "")),
            "venue": meta.get("venue"),
            "agendas": meta.get("agendas", []),
            "document": doc.get("title"),
            "document_id": doc.get("id"),
            "pdf_pages": doc.get("pdf_pages"),
            "content_slides": doc.get("content_slides"),
            "chapters": meta.get("chapters", []),
            "materials_url": meta.get("materials_url"),
            "minutes": meta.get("minutes", {}),
        })
        chapter_title = {c["key"]: c["title"] for c in meta.get("chapters", [])}
        doc_key = doc.get("key", "sanko")

        written = {}
        for md_path in sorted(meta_path.parent.glob("*/p*.md")):
            fm, body = split_frontmatter(md_path.read_text(encoding="utf-8"))
            if fm.get("pdf_page"):
                written[fm["pdf_page"]] = (fm, body)

        for s in meta.get("slides", []):
            page = s["pdf_page"]
            sid = f"{council}-{meeting}-{doc_key}-p{page:03d}"
            fm, body = written.get(page, ({}, ""))
            third = bool(fm.get("third_party_figure", s.get("third_party_figure", False)))
            slides.append({
                "id": sid,
                "council": council, "council_name": councils.get(council, {}).get("name", council),
                "meeting": meeting, "document": doc.get("id"),
                "pdf_page": page, "printed_page": s.get("printed_page"),
                "title": s.get("title"),
                "chapter_key": s.get("chapter"),
                "chapter": chapter_title.get(s.get("chapter"), s.get("chapter")),
                "slide_type": s.get("type"),
                "themes": fm.get("themes") or s.get("themes") or [],
                "terms": fm.get("terms") or s.get("terms") or [],
                "supports_topics": fm.get("supports_topics") or s.get("supports_topics") or [],
                "origin": fm.get("origin") or s.get("origin"),
                "data_source": fm.get("data_source") or s.get("data_source"),
                "note": s.get("note"),
                "text_extractable": bool(s.get("text_ok", True)),
                "third_party_figure": third,
                "has_commentary": bool(body.strip()),
                "confidence": fm.get("confidence"),
                "review_status": fm.get("review_status", "none"),
                "number_verified": bool(fm.get("number_verified", False)),
                "image": (f"images/{council}/{meeting}/{doc_key}/p{page:03d}.webp"
                          if (not third or include_third_party) else None),
                "thumb": (f"images/{council}/{meeting}/{doc_key}/p{page:03d}.thumb.webp"
                          if (not third or include_third_party) else None),
                "body_md": body,
            })

    # ---- evidence_slides の逆生成 ----
    by_topic: dict[str, list[str]] = {}
    for s in slides:
        for t in s["supports_topics"]:
            by_topic.setdefault(t, []).append(s["id"])
    for t in topics:
        t["evidence_slides"] = by_topic.get(t["title"], [])

    # ---- 用語の登場スライドを逆生成 ----
    by_term: dict[str, list[str]] = {}
    for s in slides:
        for term in s["terms"]:
            by_term.setdefault(term, []).append(s["id"])
    for g in glossary:
        names = [g.get("term")] + list(g.get("aliases") or [])
        seen: list[str] = []
        for n in names:
            for sid in by_term.get(n, []):
                if sid not in seen:
                    seen.append(sid)
        g["slides"] = seen

    return {"councils": list(councils.values()), "meetings": meetings, "slides": slides,
            "topics": topics, "timeline": timeline, "documents": documents,
            "glossary": glossary, "footer": FOOTER}


def render_bodies(data: dict) -> None:
    ids = {s["id"] for s in data["slides"]}
    for s in data["slides"]:
        md = s.pop("body_md")
        s["html"] = mdlite.render(rewrite_links(md, ids)) if md.strip() else ""
        s["search"] = " ".join(filter(None, [
            s["title"], s["chapter"], s["slide_type"], s.get("note") or "",
            " ".join(s["themes"]), " ".join(s["terms"]), " ".join(s["supports_topics"]),
            mdlite.plain(md)]))
    for g in data["glossary"]:
        md = g.pop("body_md")
        g["html"] = mdlite.render(rewrite_links(md, ids))


def copy_images(out: Path, data: dict) -> int:
    copied = 0
    for s in data["slides"]:
        for key in ("image", "thumb"):
            rel = s.get(key)
            if not rel:
                continue
            src = ROOT / rel
            if not src.exists():
                s[key] = None
                continue
            dest = out / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            copied += 1
    return copied


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="dist")
    ap.add_argument("--include-third-party", action="store_true",
                    help="third_party_figure: true のスライド画像も配信する（既定は配信しない）")
    args = ap.parse_args(argv)

    out = (ROOT / args.out).resolve()
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    data = collect(args.include_third_party)
    render_bodies(data)
    n_images = copy_images(out, data)

    # YAMLの日付は datetime.date になるので文字列に落とす
    (out / "data.json").write_text(
        json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str),
        encoding="utf-8")
    shutil.copy2(TEMPLATE, out / "index.html")
    (out / ".nojekyll").write_text("", encoding="utf-8")

    written = sum(1 for s in data["slides"] if s["has_commentary"])
    unverified = sum(1 for s in data["slides"] if s["has_commentary"] and not s["number_verified"])
    third = sum(1 for s in data["slides"] if s["third_party_figure"])
    print(f"{out.relative_to(ROOT)}/ を生成した")
    print(f"  スライド  {len(data['slides'])}枚（解説あり {written}枚 / 数値未検証 {unverified}枚）")
    print(f"  画像      {n_images}ファイル"
          f"{'（第三者図版 %d枚は配信しない）' % third if third and not args.include_third_party else ''}")
    print(f"  用語 {len(data['glossary'])} / 論点 {len(data['topics'])} / "
          f"年表 {len(data['timeline'])} / 文書 {len(data['documents'])}")
    size = sum(p.stat().st_size for p in out.rglob("*") if p.is_file())
    print(f"  合計 {size/1024/1024:.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
