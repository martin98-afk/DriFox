#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────
#  sync_tag.sh — 在 dev 与 pyside6 两个分支同步推送同一个 tag
#  用法：scripts/sync_tag.sh v0.5.6
#
#  ┌─────────────────────────────────────────────────────────┐
#  │ 双轨发布流程                                            │
#  │  dev 分支     = PyQt5 主版本（v0.5.x 系列已发布）       │
#  │  pyside6 分支 = PySide6 新版本（当前活跃开发）           │
#  │  两分支独立演进，**共用同一版本号**（如都打 v0.5.6）    │
#  └─────────────────────────────────────────────────────────┘
#
#  产物命名（同一 tag v0.5.6）：
#    Drifox-Windows-Setup-v0.5.6-qt5.exe       # dev 分支（PyQt5）
#    Drifox-Windows-Setup-v0.5.6-pyside6.exe   # pyside6 分支（PySide6）
#    Drifox-macOS-v0.5.6-qt5.dmg / -pyside6.dmg
#    Drifox-Linux-v0.5.6-qt5.tar.gz / -pyside6.tar.gz
#    -dev 后缀被刻意省略，避免污染历史 PyQt5 安装包文件名。
#
#  流程：
#    1) 检查工作树干净 + 当前在 dev/pyside6 + CHANGELOG.md 有 vX.Y.Z 条目
#    2) 在当前分支 HEAD 打 tag
#    3) 切到另一分支 fetch + 打同名 tag
#    4) 推送 tag → GitHub Actions 在两分支的 tag 触发 release.yml
#    5) release.yml 根据分支自动加 -qt5 或 -pyside6 后缀，产物共存于同一 Release
#
#  紧急回滚：
#    git tag -d v0.5.6 && git push origin :refs/tags/v0.5.6
#    gh release delete v0.5.6 --yes
# ──────────────────────────────────────────────────────────────
set -euo pipefail

TARGET_TAG="${1:-}"
if [[ -z "$TARGET_TAG" ]]; then
    echo "用法: $0 <tag>" >&2
    echo "示例: $0 v0.5.6" >&2
    exit 1
fi

# 必须从 dev 或 pyside6 之一执行
CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
case "$CURRENT_BRANCH" in
    dev|pyside6) ;;
    *)
        echo "❌ 当前分支 $CURRENT_BRANCH 不在白名单（dev/pyside6）" >&2
        echo "   请先 checkout 到 dev 或 pyside6 再执行" >&2
        exit 1
        ;;
esac

# 工作树必须干净
if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "❌ 工作树有未提交修改，请先 commit 或 stash" >&2
    exit 1
fi

# 拉取最新
echo "🔄 同步远程..."
git fetch --tags origin

# 检查 tag 是否已存在
if git rev-parse "$TARGET_TAG" >/dev/null 2>&1; then
    echo "❌ tag $TARGET_TAG 已存在，请改个版本号" >&2
    exit 1
fi

# 在另一分支打同名 tag（基于该分支 HEAD）
OTHER_BRANCH="dev"
[[ "$CURRENT_BRANCH" == "dev" ]] && OTHER_BRANCH="pyside6"

# 检查 CHANGELOG.md 是否已有对应条目（避免漏更新文档）
if ! grep -qE "^## \[${TARGET_TAG//./\\.}\]" CHANGELOG.md 2>/dev/null; then
    echo "⚠️  CHANGELOG.md 里没有 ${TARGET_TAG} 的条目，确定要继续吗？" >&2
    read -rp "   继续？[y/N] " ans
    [[ "$ans" =~ ^[Yy]$ ]] || { echo "已取消"; exit 1; }
fi

echo "🏷️  在 $CURRENT_BRANCH 上打 tag $TARGET_TAG ..."
git tag -a "$TARGET_TAG" -m "$TARGET_TAG"

echo "🏷️  切到 $OTHER_BRANCH 打同名 tag ..."
git checkout "$OTHER_BRANCH"
git pull --ff-only origin "$OTHER_BRANCH" || true
git tag -a "$TARGET_TAG" -m "$TARGET_TAG"

echo "📤 推送 tag 到 origin（两分支同步生效）..."
git push origin "$TARGET_TAG"

echo "✅ 完成：tag $TARGET_TAG 已同时存在于 dev 和 pyside6"
echo "   GitHub Actions 将自动构建两个版本：-qt5 与 -pyside6"

git checkout "$CURRENT_BRANCH"