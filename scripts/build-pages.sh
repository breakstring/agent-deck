#!/usr/bin/env bash
# 组装 Agent Deck 的 GitHub Pages 静态产物。
#
# 输入：唯一参数为可写的目标目录；源素材保留在仓库的 site/ 与 assets/agent-deck/ 中。
# 输出：仅生成公开播放页所需的 HTML/CSS/JS、两张图片和 .nojekyll，不携带项目源码或大视频文件。
# 副作用：在目标目录创建或覆盖同名静态文件；不会修改仓库内的源文件。

set -euo pipefail

if [[ "$#" -ne 1 ]]; then
  echo "用法：$0 <输出目录>" >&2
  exit 2
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
output_dir="$1"

install -d "$output_dir/assets"
install -m 0644 "$repo_root/site/index.html" "$output_dir/index.html"
install -m 0644 "$repo_root/site/styles.css" "$output_dir/styles.css"
install -m 0644 "$repo_root/site/app.js" "$output_dir/app.js"
install -m 0644 "$repo_root/assets/agent-deck/product-intro-v06.png" "$output_dir/assets/product-intro-v06.png"
install -m 0644 "$repo_root/assets/agent-deck/config.png" "$output_dir/assets/config.png"
touch "$output_dir/.nojekyll"
