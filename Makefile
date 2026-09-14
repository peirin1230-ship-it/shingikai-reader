# shingikai-reader
#
#   make fetch extract validate build
#
# 既定の対象は医療保険部会 第215回 参考資料。別の資料を扱うときは変数で上書きする。
#   make extract COUNCIL=hoken MEETING=216 DOCUMENT=shiryo1

PY       ?= python3
COUNCIL  ?= hoken
MEETING  ?= 215
DOCUMENT ?= sanko
OUT      ?= dist
PORT     ?= 8000

TARGET := --council $(COUNCIL) --meeting $(MEETING)
DOCTARGET := $(TARGET) --document $(DOCUMENT)

.DEFAULT_GOAL := help

## help: このヘルプを表示する
help:
	@grep -E '^## ' $(MAKEFILE_LIST) | sed 's/^## //' | awk -F': *' '{printf "  make %-14s %s\n", $$1, $$2}'

## setup: 必要な外部コマンドが入っているか確認する
setup:
	@ok=1; \
	for c in pdftotext pdftoppm pdfinfo cwebp tesseract; do \
	  if command -v $$c >/dev/null 2>&1; then printf '  %-10s OK\n' $$c; \
	  else printf '  %-10s MISSING\n' $$c; ok=0; fi; \
	done; \
	if [ $$ok -eq 0 ]; then \
	  echo; echo '  Debian/Ubuntu: sudo apt-get install -y poppler-utils webp'; \
	  echo '  macOS:         brew install poppler webp'; exit 1; \
	fi

## fetch: manifest.yaml のURLからPDFを取得しSHA256を検証する
fetch:
	$(PY) build/fetch.py $(TARGET)

## fetch-all: 全会議体・全回のPDFを取得する
fetch-all:
	$(PY) build/fetch.py --all

## extract: PDF を画像（images/）と抽出テキスト・OCR（work/）に分解する
extract: setup
	$(PY) build/extract.py $(DOCTARGET)

## extract-all: 全資料を分解する
extract-all: setup
	$(PY) build/extract.py --all

## validate: フロントマター・数値・参照キーを検証する（URL死活は --check-urls）
validate:
	$(PY) build/validate.py --all

## build: 解説Markdown + KB からサイトを生成する
build:
	$(PY) build/build.py --out $(OUT)

## serve: 生成したサイトをローカル配信する
serve: build
	@echo "http://localhost:$(PORT)/"
	@cd $(OUT) && $(PY) -m http.server $(PORT)

## site: 取得から生成まで一括で走らせる
site: fetch-all extract-all validate build

## clean: 生成物を消す（source/ の原典PDFは残す）
clean:
	rm -rf $(OUT) work images

## distclean: 原典PDFも含めて消す
distclean: clean
	rm -rf source

.PHONY: help setup fetch fetch-all extract extract-all validate build serve site clean distclean
