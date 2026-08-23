from __future__ import annotations

from textual.theme import Theme

DARK_PLUS = Theme(
    name="dark-plus",
    primary="#569cd6",
    secondary="#c586c0",
    warning="#ce9178",
    error="#f44747",
    success="#6a9955",
    accent="#dcdcaa",
    foreground="#d4d4d4",
    background="#1e1e1e",
    surface="#252526",
    panel="#2d2d30",
    boost="#37373d",
    dark=True,
)

THEME_ALIASES = {
    "dark+": "dark-plus",
    "darkplus": "dark-plus",
    "catppuccin": "catppuccin-mocha",
}

FAVORITE_THEMES = (
    "dark-plus",
    "monokai",
    "catppuccin-mocha",
    "tokyo-night",
)


def resolve_theme(name: str) -> str:
    normalized = name.strip().lower()
    return THEME_ALIASES.get(normalized, normalized)
