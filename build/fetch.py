#!/usr/bin/env python3
"""manifest.yaml のURLから原典PDFを取得し、SHA256を検証して source/ に置く。

    python3 build/fetch.py --council hoken --meeting 215
    python3 build/fetch.py --all
    python3 build/fetch.py --all --offline     # 取得せず手元のファイルを検証するだけ

source/ は .gitignore されているので、取得したPDFがリポジトリに入ることはない。

審議会資料は訂正版に差し替えられることがある。ハッシュ不一致はエラーではなく警告として扱い、
manifest.yaml の revisions に追記して新しいハッシュへ更新すること（SPEC §3 Phase 0）。
"""
from __future__ import annotations

import argparse
import hashlib
import sys
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
COUNCILS = ROOT / "councils"
SOURCE = ROOT / "source"
UA = "shingikai-reader/0.1 (+https://github.com/; personal research tool)"
CHUNK = 1 << 16


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def manifests(council: str | None, meeting: str | None):
    """対象となる manifest.yaml を列挙する。"""
    pattern = f"{council or '*'}/{meeting or '*'}/manifest.yaml"
    for path in sorted(COUNCILS.glob(pattern)):
        with path.open(encoding="utf-8") as fh:
            yield path, yaml.safe_load(fh)


def download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=120) as resp, tmp.open("wb") as out:
        while True:
            block = resp.read(CHUNK)
            if not block:
                break
            out.write(block)
    tmp.replace(dest)


def handle(manifest_path: Path, manifest: dict, doc: dict, offline: bool) -> str:
    """1資料を処理して 'ok' / 'skip' / 'warn' / 'error' を返す。"""
    council = manifest["council"]
    meeting = manifest["meeting"]
    key = doc["key"]
    label = f"{council}/{meeting}/{key}"
    dest = SOURCE / str(council) / str(meeting) / f"{key}.pdf"
    expected = doc.get("sha256")
    url = doc.get("url")

    if not url:
        print(f"  {label:24} SKIP  URLが未記入")
        return "skip"

    if dest.exists() and expected and sha256_of(dest) == expected:
        print(f"  {label:24} OK    取得済み・ハッシュ一致")
        return "ok"

    if offline:
        if not dest.exists():
            print(f"  {label:24} SKIP  --offline かつ手元にファイルがない")
            return "skip"
        actual = sha256_of(dest)
        if expected is None:
            print(f"  {label:24} WARN  manifest に sha256 がない（実測 {actual}）")
            return "warn"
        print(f"  {label:24} WARN  ハッシュ不一致\n"
              f"      manifest: {expected}\n      実測:     {actual}")
        return "warn"

    print(f"  {label:24} ...   取得中 {url}")
    try:
        download(url, dest)
    except Exception as exc:  # noqa: BLE001 - ネットワーク起因は理由を出して続行する
        print(f"  {label:24} ERROR 取得に失敗: {exc}")
        return "error"

    actual = sha256_of(dest)
    size = dest.stat().st_size
    if expected is None:
        print(f"  {label:24} WARN  取得したが manifest に sha256 がない。次を追記すること:\n"
              f"      sha256: {actual}\n      bytes: {size}")
        return "warn"
    if actual != expected:
        print(f"  {label:24} WARN  ★ハッシュ不一致。差し替えの可能性がある\n"
              f"      manifest: {expected}\n      取得物:   {actual}\n"
              f"      → manifest.yaml の revisions に追記し、この資料の数値を洗い直すこと")
        return "warn"
    print(f"  {label:24} OK    取得・検証完了（{size:,} bytes）")
    return "ok"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--council")
    ap.add_argument("--meeting")
    ap.add_argument("--document", help="資料キー（sanko / shiryo1 など）。既定は全部")
    ap.add_argument("--all", action="store_true", help="全会議体・全回を対象にする")
    ap.add_argument("--offline", action="store_true",
                    help="取得せず、手元のファイルのハッシュを検証するだけ")
    ap.add_argument("--include-inactive", action="store_true",
                    help="manifest の active: false の資料も対象にする")
    args = ap.parse_args(argv)

    if not args.all and not args.council:
        ap.error("--council か --all のどちらかを指定すること")

    counts = {"ok": 0, "skip": 0, "warn": 0, "error": 0}
    found = False
    for path, manifest in manifests(args.council, args.meeting):
        found = True
        print(f"{path.relative_to(ROOT)}")
        for doc in manifest.get("documents", []):
            if args.document and doc.get("key") != args.document:
                continue
            # --document で名指しされたものは active に関係なく扱う
            if not args.document and not args.include_inactive and not doc.get("active", True):
                continue
            counts[handle(path, manifest, doc, args.offline)] += 1

    if not found:
        print("対象の manifest.yaml が見つからない", file=sys.stderr)
        return 1

    print(f"\nok={counts['ok']} skip={counts['skip']} warn={counts['warn']} error={counts['error']}")
    return 1 if counts["error"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
