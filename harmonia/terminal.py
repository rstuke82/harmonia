"""Formatting shared by background job output."""

class Colors:
    RESET = '\033[0m'
    BOLD = '\033[1m'
    DIM = '\033[2m'
    RED = '\033[91m'
    GREEN = '\033[92m'
    CYAN = '\033[96m'
    YELLOW = '\033[93m'
    MAGENTA = '\033[95m'
    BLUE = '\033[94m'
    WHITE = '\033[97m'

    # Brand-appropriate 24-bit terminal colors.
    SPOTIFY = '\033[38;2;29;185;84m'       # #1DB954
    APPLE_MUSIC = '\033[38;2;250;45;72m'   # #FA2D48


def colored(text: str, color: str) -> str:
    """Add color/style to text for terminal output."""
    return f"{color}{text}{Colors.RESET}"


def dimmed(text) -> str:
    """Render secondary terminal metadata with lower visual emphasis."""
    return colored(str(text), Colors.DIM)


def section_header(title: str) -> str:
    """Return one consistent lightweight section divider."""
    rule = colored("─" * 50, Colors.DIM)
    heading = colored(title, Colors.BOLD + Colors.CYAN)
    return f"\n{rule}\n{heading}\n{rule}"


def auto_sync_display(
    enabled: bool,
    *,
    symbol: bool = False,
) -> str:
    """Color automatic-sync state consistently."""
    if enabled:
        label = "● ON" if symbol else "ON"
        return colored(label, Colors.GREEN)

    label = "● OFF" if symbol else "OFF"
    return colored(label, Colors.RED)




def provenance_display(value: str) -> str:
    """Color automatic/manual/legacy provenance consistently."""
    normalized = str(value or "legacy").strip().lower()
    colors = {
        "automatic": Colors.GREEN,
        "manual": Colors.CYAN,
        "legacy": Colors.YELLOW,
    }
    return colored(
        normalized,
        colors.get(normalized, Colors.WHITE),
    )




