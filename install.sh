#!/usr/bin/env bash
# DocMind 一键安装脚本
# 用法: curl -sL https://raw.githubusercontent.com/yzy806806/docmind/master/install.sh | bash
# 或: bash install.sh
set -euo pipefail

# ── 基本变量 ──────────────────────────────────────────────────
REPO="https://github.com/yzy806806/docmind.git"
INSTALL_DIR="${DOCMIND_INSTALL_DIR:-/opt/docmind}"
SERVICE_NAME="docmind"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
PYTHON="${DOCMIND_PYTHON:-python3}"

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }

# ── 前置检查 ──────────────────────────────────────────────────
check_root() {
    if [[ $EUID -ne 0 ]]; then
        error "请以 root 用户运行此脚本"
        exit 1
    fi
}

check_python() {
    if ! command -v python3 &>/dev/null; then
        error "未找到 python3，请先安装 Python 3.11+"
        exit 1
    fi
    local ver
    ver=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
    local major minor
    major=$(echo "$ver" | cut -d. -f1)
    minor=$(echo "$ver" | cut -d. -f2)
    if [[ $major -lt 3 ]] || { [[ $major -eq 3 ]] && [[ $minor -lt 11 ]]; }; then
        error "需要 Python 3.11+，当前为 $ver"
        exit 1
    fi
    info "Python $ver 检测通过"
}

check_uv() {
    if ! command -v uv &>/dev/null; then
        info "安装 uv 包管理器..."
        curl -sL https://astral.sh/uv/install.sh | bash
        export PATH="$HOME/.local/bin:$PATH"
        # 确保当前 shell 能找到 uv
        if ! command -v uv &>/dev/null; then
            export PATH="/usr/local/bin:$PATH"
        fi
    fi
    info "uv $(uv --version 2>/dev/null || echo '已安装') 检测通过"
}

# ── 安装步骤 ──────────────────────────────────────────────────
clone_repo() {
    if [[ -d "$INSTALL_DIR/.git" ]]; then
        info "仓库已存在，拉取最新代码..."
        cd "$INSTALL_DIR"
        git pull --ff-only
    else
        info "克隆仓库到 $INSTALL_DIR ..."
        mkdir -p "$(dirname "$INSTALL_DIR")"
        git clone "$REPO" "$INSTALL_DIR"
        cd "$INSTALL_DIR"
    fi
}

install_deps() {
    cd "$INSTALL_DIR"
    info "安装 Python 依赖..."
    uv sync
}

create_service() {
    local venv_python="$INSTALL_DIR/.venv/bin/python"
    if [[ ! -f "$venv_python" ]]; then
        # uv venv 可能在不同路径
        venv_python="$(cd "$INSTALL_DIR" && uv run which python 2>/dev/null || echo "$INSTALL_DIR/.venv/bin/python")"
    fi

    info "创建 systemd service..."
    cat > "$SERVICE_FILE" << EOF
[Unit]
Description=DocMind — AI-Powered Document Knowledge Base
After=network.target

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR
ExecStart=$venv_python -m src.web.server
Restart=on-failure
RestartSec=5
Environment=PYTHONUNBUFFERED=1

# 可选：通过环境变量覆盖配置
# Environment=DOCMIND_PORT=9980
# Environment=DOCMIND_DEBUG=false

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    systemctl enable "$SERVICE_NAME"
    info "systemd service 已创建并设为开机自启"
}

start_service() {
    info "启动 DocMind 服务..."
    systemctl restart "$SERVICE_NAME"
    sleep 2

    if systemctl is-active --quiet "$SERVICE_NAME"; then
        info "DocMind 服务已启动 ✓"
    else
        error "DocMind 服务启动失败，请检查日志："
        journalctl -u "$SERVICE_NAME" -n 20 --no-pager
        exit 1
    fi
}

show_info() {
    local port
    port=$(grep -oP 'DOCMIND_PORT.*?(\d+)' "$SERVICE_FILE" 2>/dev/null | grep -oP '\d+' || echo "9980")
    echo ""
    echo -e "${GREEN}═══════════════════════════════════════════════${NC}"
    echo -e "${GREEN}  DocMind 安装完成！${NC}"
    echo -e "${GREEN}═══════════════════════════════════════════════${NC}"
    echo ""
    echo "  访问地址:  http://<服务器IP>:$port"
    echo "  安装目录:  $INSTALL_DIR"
    echo "  服务状态:  systemctl status $SERVICE_NAME"
    echo "  查看日志:  journalctl -u $SERVICE_NAME -f"
    echo "  重启服务:  systemctl restart $SERVICE_NAME"
    echo ""
    echo "  首次使用请在 WebUI 设置页面配置 LLM 和认证。"
    echo ""
}

# ── 主流程 ────────────────────────────────────────────────────
main() {
    echo ""
    echo "  DocMind 一键安装"
    echo ""

    check_root
    check_python
    check_uv
    clone_repo
    install_deps
    create_service
    start_service
    show_info
}

main "$@"
