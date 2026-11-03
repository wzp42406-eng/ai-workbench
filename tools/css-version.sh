#!/usr/bin/env bash
# 给 index.html 里的 tokens.css / site.css 加内容版本号（?v=8 位哈希），防止浏览器缓存旧样式。
# 改了任何 CSS 后、提交前运行：bash tools/css-version.sh
set -euo pipefail
cd "$(dirname "$0")/.."
for f in tokens.css site.css; do
  v=$(sha256sum "$f" | cut -c1-8)
  sed -i -E "s#href=\"$f(\\?v=[0-9a-f]+)?\"#href=\"$f?v=$v\"#" index.html
done
grep -o "<link rel=\"stylesheet\"[^>]*>" index.html
