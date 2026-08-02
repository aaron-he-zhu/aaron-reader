#!/bin/sh
set -eu

LABEL=com.aaron.reader
LAUNCHCTL=/bin/launchctl
ID=/usr/bin/id
PLUTIL=/usr/bin/plutil
PYTHON=/usr/bin/python3
MKTEMP=/usr/bin/mktemp
UI_LANG=${AARON_READER_LANG:-en}

say() {
  if [ "$UI_LANG" = "zh-CN" ]; then
    printf '%s\n' "$2"
  else
    printf '%s\n' "$1"
  fi
}

say_err() {
  say "$1" "$2" >&2
}

usage() {
  say_err \
    "Usage: $0 [interval-seconds, 300-604800]" \
    "用法：$0 [间隔秒数，300-604800]"
}

if [ "$#" -gt 1 ]; then
  usage
  exit 2
fi

USER_ID=$($ID -u)
if [ "$USER_ID" -eq 0 ] || [ -n "${SUDO_UID:-}" ] || [ -n "${SUDO_USER:-}" ]; then
  say_err \
    "Install directly as the currently logged-in user; do not use sudo or root." \
    "请以当前登录用户直接安装，不要使用 sudo 或 root。"
  exit 2
fi
case "${HOME:-}" in
  /*) ;;
  *)
    say_err \
      "HOME must be the absolute path of the currently logged-in user." \
      "HOME 必须是当前登录用户的绝对路径。"
    exit 2
    ;;
esac

READER_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
RUNTIME_ROOT="$HOME/Library/Application Support/Aaron Reader"
INTERVAL=${1:-3600}
case "$INTERVAL" in
  *[!0-9]*|'')
    usage
    exit 2
    ;;
esac
if [ "$INTERVAL" -lt 300 ] || [ "$INTERVAL" -gt 604800 ]; then
  say_err \
    "The interval must be between 300 and 604800 seconds." \
    "间隔必须在 300 到 604800 秒之间。"
  exit 2
fi

AGENTS_DIR="$HOME/Library/LaunchAgents"
PLIST_PATH="$AGENTS_DIR/$LABEL.plist"
SERVICE="gui/$USER_ID/$LABEL"
DOMAIN="gui/$USER_ID"
TEMP_PLIST=
BACKUP_PLIST=
WAS_LOADED=0
HAD_PLIST=0
OLD_STOPPED=0
PLIST_REPLACED=0
INSTALL_SUCCEEDED=0

cleanup() {
  EXIT_STATUS=$?
  trap - 0 1 2 15
  if [ "$INSTALL_SUCCEEDED" -ne 1 ]; then
    RESTORE_READY=0
    if [ "$PLIST_REPLACED" -eq 1 ]; then
      if "$LAUNCHCTL" print "$SERVICE" >/dev/null 2>&1; then
        "$LAUNCHCTL" bootout "$SERVICE" >/dev/null 2>&1 || \
          say_err \
            "Warning: could not stop the partially installed new job." \
            "警告：未能停止未完成安装的新任务。"
      fi
      if [ "$HAD_PLIST" -eq 1 ] && [ -n "$BACKUP_PLIST" ]; then
        say_err \
          "Installation did not complete; restoring the previous plist..." \
          "安装未完成，正在恢复之前的 plist……"
        if /bin/mv -f "$BACKUP_PLIST" "$PLIST_PATH"; then
          BACKUP_PLIST=
          RESTORE_READY=1
        else
          say_err \
            "Warning: the previous plist could not be restored. Its backup remains at: $BACKUP_PLIST" \
            "警告：旧 plist 恢复失败，备份保留在：$BACKUP_PLIST"
        fi
      else
        /bin/rm -f "$PLIST_PATH"
      fi
    elif [ -f "$PLIST_PATH" ]; then
      RESTORE_READY=1
    fi
    if [ "$WAS_LOADED" -eq 1 ] && [ "$OLD_STOPPED" -eq 1 ] && \
       [ "$RESTORE_READY" -eq 1 ]; then
      if ! "$LAUNCHCTL" bootstrap "$DOMAIN" "$PLIST_PATH" >/dev/null 2>&1; then
        say_err \
          "Warning: the previous plist is available, but its job could not be reloaded; inspect it manually." \
          "警告：旧 plist 可用，但旧任务无法重新加载；请手动检查。"
      fi
    fi
  fi
  if [ -n "$TEMP_PLIST" ]; then
    /bin/rm -f "$TEMP_PLIST"
  fi
  if [ -n "$BACKUP_PLIST" ] && \
     { [ "$INSTALL_SUCCEEDED" -eq 1 ] || [ "$PLIST_REPLACED" -eq 0 ]; }; then
    /bin/rm -f "$BACKUP_PLIST"
  fi
  exit "$EXIT_STATUS"
}
trap cleanup 0
trap 'exit 129' 1
trap 'exit 130' 2
trap 'exit 143' 15

for TOOL in "$LAUNCHCTL" "$ID" "$PLUTIL" "$PYTHON" "$MKTEMP"; do
  if [ ! -x "$TOOL" ]; then
    say_err "Required system tool is missing: $TOOL" "缺少系统工具：$TOOL"
    exit 1
  fi
done
if [ ! -x "$READER_ROOT/aaron-reader" ]; then
  say_err \
    "Executable entry point is missing: $READER_ROOT/aaron-reader" \
    "缺少可执行入口：$READER_ROOT/aaron-reader"
  exit 1
fi
if [ ! -f "$READER_ROOT/config/sources.json" ] || \
   [ ! -f "$READER_ROOT/src/aaron_reader/__main__.py" ] || \
   [ ! -f "$READER_ROOT/scripts/install_runtime.py" ] || \
   [ ! -f "$READER_ROOT/scripts/render_launchd.py" ]; then
  say_err \
    "The project directory is incomplete; cannot install the LaunchAgent: $READER_ROOT" \
    "项目目录不完整，无法安装 LaunchAgent：$READER_ROOT"
  exit 1
fi

say "Running the pre-installation check..." "正在运行安装前检查……"
"$READER_ROOT/aaron-reader" doctor

/bin/mkdir -p "$AGENTS_DIR" "$RUNTIME_ROOT"

if "$LAUNCHCTL" print "$SERVICE" >/dev/null 2>&1; then
  WAS_LOADED=1
fi

if [ -e "$PLIST_PATH" ] || [ -L "$PLIST_PATH" ]; then
  if [ -L "$PLIST_PATH" ] || [ ! -f "$PLIST_PATH" ]; then
    say_err \
      "Refusing to replace a plist path that is not a regular file: $PLIST_PATH" \
      "拒绝替换非普通 plist 文件：$PLIST_PATH"
    exit 1
  fi
  HAD_PLIST=1
  BACKUP_PLIST=$($MKTEMP "$AGENTS_DIR/.$LABEL.backup.XXXXXX")
  /bin/cp -p "$PLIST_PATH" "$BACKUP_PLIST"
elif [ "$WAS_LOADED" -eq 1 ]; then
  say_err \
    "The job is loaded but its installed plist is missing. Run the uninstall script first so rollback remains possible." \
    "任务已加载但安装 plist 缺失；为避免无法回滚，请先运行卸载脚本。"
  exit 1
fi

if [ "$WAS_LOADED" -eq 1 ]; then
  say "Safely stopping the previous job..." "正在安全停止旧任务……"
  if ! "$LAUNCHCTL" bootout "$SERVICE"; then
    say_err \
      "The previous job could not be stopped; the plist was not modified." \
      "旧任务无法停止；plist 未修改。"
    exit 1
  fi
  OLD_STOPPED=1
fi

say \
  "Installing the background runtime in Application Support..." \
  "正在把后台运行环境安装到 Application Support……"
$PYTHON "$READER_ROOT/scripts/install_runtime.py" \
  --source-root "$READER_ROOT" \
  --runtime-root "$RUNTIME_ROOT"
"$RUNTIME_ROOT/aaron-reader" doctor
"$RUNTIME_ROOT/aaron-reader" render

TEMP_PLIST=$($MKTEMP "$AGENTS_DIR/.$LABEL.plist.XXXXXX")
$PYTHON "$READER_ROOT/scripts/render_launchd.py" \
  --root "$RUNTIME_ROOT" \
  --output "$TEMP_PLIST" \
  --interval "$INTERVAL"
$PLUTIL -lint "$TEMP_PLIST"
/bin/chmod 600 "$TEMP_PLIST"

# TEMP_PLIST and the target are in the same directory, so mv replaces the file
# atomically on the same filesystem.
/bin/mv -f "$TEMP_PLIST" "$PLIST_PATH"
TEMP_PLIST=
PLIST_REPLACED=1

if ! "$LAUNCHCTL" bootstrap "$DOMAIN" "$PLIST_PATH"; then
  say_err \
    "The new job could not be loaded; the installer will try to restore the previous installation while exiting." \
    "新任务加载失败；退出时将尝试恢复之前的安装。"
  exit 1
fi
INSTALL_SUCCEEDED=1

if [ -n "$BACKUP_PLIST" ]; then
  /bin/rm -f "$BACKUP_PLIST"
  BACKUP_PLIST=
fi

say \
  "Aaron Reader is installed and will sync every $INTERVAL seconds." \
  "已安装 Aaron Reader，每 $INTERVAL 秒同步一次。"
say \
  "RunAtLoad is enabled; launchd will start the first sync automatically without another forced start." \
  "RunAtLoad 已启用；launchd 会自动开始首轮同步，无需再次强制启动。"
say "Configuration: $PLIST_PATH" "配置：$PLIST_PATH"
say \
  "Installed runtime: $RUNTIME_ROOT" \
  "已安装运行环境：$RUNTIME_ROOT"
say \
  "Reading page: $RUNTIME_ROOT/public/index.html" \
  "阅读页面：$RUNTIME_ROOT/public/index.html"
say \
  "Standard output: $RUNTIME_ROOT/data/launchd.log" \
  "标准输出：$RUNTIME_ROOT/data/launchd.log"
say \
  "Error output: $RUNTIME_ROOT/data/launchd.error.log" \
  "错误输出：$RUNTIME_ROOT/data/launchd.error.log"
