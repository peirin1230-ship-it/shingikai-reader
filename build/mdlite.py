#!/usr/bin/env python3
"""解説Markdownを描画するための小さなレンダラ。

外部依存を増やしたくないので必要な範囲だけ自前で持つ。対応するのは
見出し / 段落 / 表 / 箇条書き / 番号付きリスト / 引用 / 水平線 /
強調 / インラインコード / リンク / <br>。
"""
from __future__ import annotations

import html
import re

_INLINE_CODE = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_BARE_URL = re.compile(r"(?<![\"'=(])(https?://[^\s<>\"')\]]+)")


def inline(text: str) -> str:
    """インライン要素を描画する。エスケープしてから戻していく。"""
    placeholders: list[str] = []

    def stash(markup: str) -> str:
        placeholders.append(markup)
        return f"\x00{len(placeholders) - 1}\x00"

    # <br> は表のセル内で使うので通す
    text = text.replace("<br>", stash("<br>"))
    text = _INLINE_CODE.sub(lambda m: stash(f"<code>{html.escape(m.group(1))}</code>"), text)
    text = _LINK.sub(
        lambda m: stash(f'<a href="{html.escape(m.group(2), quote=True)}">{html.escape(m.group(1))}</a>'),
        text)
    text = html.escape(text)
    text = _BOLD.sub(r"<strong>\1</strong>", text)
    text = _BARE_URL.sub(lambda m: f'<a href="{m.group(1)}">{m.group(1)}</a>', text)

    for i, markup in enumerate(placeholders):
        text = text.replace(f"\x00{i}\x00", markup)
    return text


def _table(lines: list[str]) -> str:
    def cells(row: str) -> list[str]:
        row = row.strip()
        if row.startswith("|"):
            row = row[1:]
        if row.endswith("|"):
            row = row[:-1]
        return [c.strip() for c in row.split("|")]

    head = cells(lines[0])
    body = [cells(r) for r in lines[2:]]
    out = ['<div class="tablewrap"><table><thead><tr>']
    out += [f"<th>{inline(c)}</th>" for c in head]
    out.append("</tr></thead><tbody>")
    for row in body:
        out.append("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in row) + "</tr>")
    out.append("</tbody></table></div>")
    return "".join(out)


def render(md: str) -> str:
    lines = md.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    i = 0
    para: list[str] = []

    def flush_para() -> None:
        if para:
            out.append(f"<p>{inline(' '.join(para))}</p>")
            para.clear()

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            flush_para()
            i += 1
            continue

        m = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if m:
            flush_para()
            level = len(m.group(1))
            text = m.group(2)
            anchor = re.sub(r"[^\w一-龥ぁ-んァ-ヶー]+", "-", text).strip("-")
            out.append(f'<h{level} id="{html.escape(anchor, quote=True)}">{inline(text)}</h{level}>')
            i += 1
            continue

        if re.match(r"^(-{3,}|\*{3,}|_{3,})$", stripped):
            flush_para()
            out.append("<hr>")
            i += 1
            continue

        # 表: ヘッダ行の次が区切り行
        if (stripped.startswith("|") and i + 1 < len(lines)
                and re.match(r"^\|?[\s:|-]+\|[\s:|-]*$", lines[i + 1].strip())):
            flush_para()
            block = [lines[i], lines[i + 1]]
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                block.append(lines[i])
                i += 1
            out.append(_table(block))
            continue

        if re.match(r"^[-*]\s+", stripped):
            flush_para()
            items: list[str] = []
            while i < len(lines) and re.match(r"^\s*[-*]\s+", lines[i]):
                items.append(re.sub(r"^\s*[-*]\s+", "", lines[i]))
                i += 1
                # 継続行（インデントされた行）を同じ項目に足す
                while i < len(lines) and re.match(r"^\s{2,}\S", lines[i]) and \
                        not re.match(r"^\s*[-*]\s+", lines[i]):
                    items[-1] += " " + lines[i].strip()
                    i += 1
            out.append("<ul>" + "".join(f"<li>{inline(x)}</li>" for x in items) + "</ul>")
            continue

        if re.match(r"^\d+\.\s+", stripped):
            flush_para()
            items = []
            while i < len(lines) and re.match(r"^\s*\d+\.\s+", lines[i]):
                items.append(re.sub(r"^\s*\d+\.\s+", "", lines[i]))
                i += 1
                while i < len(lines) and re.match(r"^\s{3,}\S", lines[i]) and \
                        not re.match(r"^\s*\d+\.\s+", lines[i]):
                    items[-1] += " " + lines[i].strip()
                    i += 1
            out.append("<ol>" + "".join(f"<li>{inline(x)}</li>" for x in items) + "</ol>")
            continue

        if stripped.startswith(">"):
            flush_para()
            quote = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                quote.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            out.append(f"<blockquote>{render(chr(10).join(quote))}</blockquote>")
            continue

        para.append(stripped)
        i += 1

    flush_para()
    return "\n".join(out)


def plain(md: str) -> str:
    """検索インデックス用に記号を落としたテキストを返す。"""
    text = re.sub(r"```.*?```", " ", md, flags=re.S)
    text = _LINK.sub(r"\1", text)
    text = re.sub(r"[#*`>|_\-]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()
