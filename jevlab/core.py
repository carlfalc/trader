"""Shared bits: where results go, and the terminal styling."""

from __future__ import annotations

from pathlib import Path

from rich import box
from rich.console import Console
from rich.panel import Panel

RESULTS = Path(__file__).resolve().parent.parent / "results"
console = Console(highlight=False)


def header(title: str, subtitle: str) -> None:
    console.print()
    console.print(Panel(f"[dim]{subtitle}[/]", title=f"[bold #8b7bff]{title}[/]", title_align="left",
                        border_style="#3a3f5c", box=box.ROUNDED, padding=(0, 2)))
