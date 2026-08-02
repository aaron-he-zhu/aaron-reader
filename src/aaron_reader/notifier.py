import platform
import subprocess
from typing import Dict

from .i18n import translate


def notify_new_articles(
    total: int, source_counts: Dict[str, int], language: str = "en"
) -> bool:
    if total <= 0 or platform.system() != "Darwin":
        return False
    details = translate("notifier.separator", language).join(
        "%s %d" % (slug, count) for slug, count in sorted(source_counts.items())
    )
    message = translate("notifier.new_articles", language, total=total)
    if details:
        message += translate("notifier.details", language, details=details)
    script = "display notification %s with title %s" % (
        _apple_script_string(message),
        _apple_script_string("Aaron Reader"),
    )
    try:
        result = subprocess.run(
            ["/usr/bin/osascript", "-e", script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _apple_script_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return '"%s"' % escaped
