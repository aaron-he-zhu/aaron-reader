#!/bin/sh
set -eu

LABEL=com.aaron.reader
LAUNCHCTL=/bin/launchctl
ID=/usr/bin/id
PLUTIL=/usr/bin/plutil
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
    "Check status directly as the currently logged-in user; do not use sudo or root." \
    "请以当前登录用户直接查看状态，不要使用 sudo 或 root。"
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
EXPECTED_RUNTIME_ROOT="$HOME/Library/Application Support/Aaron Reader"
PLIST_PATH="$HOME/Library/LaunchAgents/$LABEL.plist"
SERVICE="gui/$USER_ID/$LABEL"

if [ ! -f "$PLIST_PATH" ]; then
  say_err \
    "The installed Aaron Reader plist does not exist: $PLIST_PATH" \
    "Aaron Reader 的安装 plist 不存在：$PLIST_PATH"
  exit 1
fi
if ! "$PLUTIL" -lint "$PLIST_PATH" >/dev/null; then
  say_err \
    "The installed Aaron Reader plist is invalid; reinstall the LaunchAgent." \
    "Aaron Reader 的安装 plist 无效，请重新安装。"
  exit 1
fi
INSTALLED_ROOT=$($PLUTIL -extract WorkingDirectory raw -o - "$PLIST_PATH" 2>/dev/null || true)
if [ -z "$INSTALLED_ROOT" ] || [ "$INSTALLED_ROOT" != "$EXPECTED_RUNTIME_ROOT" ]; then
  say_err \
    "The plist does not use the expected Application Support runtime." \
    "plist 未使用预期的 Application Support 运行环境。"
  say_err \
    "Installed directory: ${INSTALLED_ROOT:-unknown}" \
    "已安装目录：${INSTALLED_ROOT:-未知}"
  say_err "Expected runtime: $EXPECTED_RUNTIME_ROOT" "预期运行环境：$EXPECTED_RUNTIME_ROOT"
  say_err \
    "Run install-launchd.sh again from the current directory." \
    "请在当前目录重新运行 install-launchd.sh。"
  exit 1
fi
if [ ! -f "$INSTALLED_ROOT/.source-root" ] || \
   [ "$(/bin/cat "$INSTALLED_ROOT/.source-root")" != "$READER_ROOT" ]; then
  say_err \
    "The installed runtime belongs to another checkout; reinstall from this directory." \
    "已安装运行环境来自另一份仓库；请在当前目录重新安装。"
  exit 1
fi

if ! JOB_STATUS=$("$LAUNCHCTL" print "$SERVICE"); then
  say_err \
    "The Aaron Reader launchd job is not installed or is not running." \
    "Aaron Reader 的 launchd 任务未安装或未运行。"
  exit 1
fi
printf '%s\n\n' "$JOB_STATUS"
LAST_EXIT_CODE=$(printf '%s\n' "$JOB_STATUS" | /usr/bin/sed -n \
  's/^[[:space:]]*last exit code = \(-\{0,1\}[0-9][0-9]*\)$/\1/p' | /usr/bin/tail -n 1)
if [ -n "$LAST_EXIT_CODE" ] && [ "$LAST_EXIT_CODE" -ne 0 ]; then
  say_err \
    "The last scheduled sync exited with code $LAST_EXIT_CODE; inspect $INSTALLED_ROOT/data/launchd.error.log." \
    "上一次定时同步以状态码 $LAST_EXIT_CODE 退出；请检查 $INSTALLED_ROOT/data/launchd.error.log。"
  "$INSTALLED_ROOT/aaron-reader" status --strict || true
  exit 1
fi
"$INSTALLED_ROOT/aaron-reader" status --strict
