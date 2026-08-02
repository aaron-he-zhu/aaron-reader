#!/bin/sh
set -eu

LABEL=com.aaron.reader
LAUNCHCTL=/bin/launchctl
ID=/usr/bin/id
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

if [ "$#" -ne 0 ]; then
  say_err "Usage: $0" "用法：$0"
  exit 2
fi
USER_ID=$($ID -u)
if [ "$USER_ID" -eq 0 ] || [ -n "${SUDO_UID:-}" ] || [ -n "${SUDO_USER:-}" ]; then
  say_err \
    "Uninstall directly as the currently logged-in user; do not use sudo or root." \
    "请以当前登录用户直接卸载，不要使用 sudo 或 root。"
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

PLIST_PATH="$HOME/Library/LaunchAgents/$LABEL.plist"
SERVICE="gui/$USER_ID/$LABEL"
if "$LAUNCHCTL" print "$SERVICE" >/dev/null 2>&1; then
  if ! "$LAUNCHCTL" bootout "$SERVICE"; then
    say_err \
      "The job could not be stopped. The plist was not moved, avoiding an orphaned running job." \
      "任务无法停止；为避免留下运行中的孤立任务，plist 未移动。"
    exit 1
  fi
fi
if [ -e "$PLIST_PATH" ] || [ -L "$PLIST_PATH" ]; then
  /bin/mkdir -p "$HOME/.Trash"
  TRASH_PATH=$($MKTEMP "$HOME/.Trash/$LABEL.$(/bin/date +%Y%m%d-%H%M%S).XXXXXX")
  if ! /bin/mv -f "$PLIST_PATH" "$TRASH_PATH"; then
    /bin/rm -f "$TRASH_PATH"
    say_err \
      "Could not move the plist to the Trash: $PLIST_PATH" \
      "无法把 plist 移到废纸篓：$PLIST_PATH"
    exit 1
  fi
  say \
    "Uninstalled. The previous plist was moved to: $TRASH_PATH" \
    "已卸载；原 plist 已移至：$TRASH_PATH"
else
  say "Aaron Reader is not installed." "Aaron Reader 未安装。"
fi
say \
  "The installed runtime, database, generated static files, and logs in Application Support were not deleted." \
  "Application Support 中的运行环境、数据库、静态输出和日志均未删除。"
