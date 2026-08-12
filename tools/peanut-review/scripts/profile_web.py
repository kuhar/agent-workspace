#!/usr/bin/env python3
"""Profile peanut-review session pages with an optional Playwright install."""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.stderr.write(
        "Playwright is optional. Install it in a disposable environment with:\n"
        "  python -m pip install playwright\n"
        "  python -m playwright install chromium\n"
    )
    raise SystemExit(2)


METRIC_NAMES = {"TaskDuration", "LayoutDuration", "RecalcStyleDuration"}


def _metrics(cdp) -> dict[str, float]:
    raw = cdp.send("Performance.getMetrics")["metrics"]
    return {
        item["name"]: float(item["value"])
        for item in raw
        if item["name"] in METRIC_NAMES
    }


def _delta(after: dict[str, float], before: dict[str, float], name: str) -> float:
    return round((after.get(name, 0.0) - before.get(name, 0.0)) * 1000, 1)


def profile_once(browser, url: str, settle_ms: int) -> dict:
    page = browser.new_page()
    cdp = page.context.new_cdp_session(page)
    cdp.send("Performance.enable")
    cdp.send("Network.enable")
    encoded_bytes = 0

    def loading_finished(params) -> None:
        nonlocal encoded_bytes
        encoded_bytes += int(params.get("encodedDataLength", 0))

    cdp.on("Network.loadingFinished", loading_finished)
    page.add_init_script("""
      window.__prLongTasks = [];
      new PerformanceObserver((list) => {
        for (const entry of list.getEntries()) {
          window.__prLongTasks.push({start: entry.startTime, duration: entry.duration});
        }
      }).observe({type: "longtask", buffered: true});
    """)

    before = _metrics(cdp)
    response = page.goto(url, wait_until="domcontentloaded")
    page.wait_for_timeout(250)
    loaded = _metrics(cdp)
    navigation = page.evaluate("""() => {
      const nav = performance.getEntriesByType("navigation")[0];
      return {
        response_start_ms: nav ? nav.responseStart : 0,
        dom_content_loaded_ms: nav ? nav.domContentLoadedEventEnd : 0,
        transfer_size: nav ? nav.transferSize : 0,
      };
    }""")
    shape = page.evaluate("""() => ({
      elements: document.getElementsByTagName("*").length,
      files: document.querySelectorAll(".file").length,
      diff_rows: document.querySelectorAll(".line").length,
      spans: document.querySelectorAll("span").length,
      html_bytes: new TextEncoder().encode(document.documentElement.outerHTML).length,
    })""")
    initial_bytes = encoded_bytes
    long_task_mark = page.evaluate("window.__prLongTasks.length")
    poll_before = _metrics(cdp)
    page.wait_for_timeout(settle_ms)
    poll_after = _metrics(cdp)
    poll_long_tasks = page.evaluate(
        "(mark) => window.__prLongTasks.slice(mark)", long_task_mark,
    )

    scroll_before = _metrics(cdp)
    page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
    page.evaluate(
        "() => new Promise((resolve) => "
        "requestAnimationFrame(() => requestAnimationFrame(resolve)))"
    )
    scroll_after = _metrics(cdp)
    all_long_tasks = page.evaluate("window.__prLongTasks")

    result = {
        "url": url,
        "status": response.status if response else None,
        "response_start_ms": round(navigation["response_start_ms"], 1),
        "dom_content_loaded_ms": round(navigation["dom_content_loaded_ms"], 1),
        "main_task_ms": _delta(loaded, before, "TaskDuration"),
        "layout_ms": _delta(loaded, before, "LayoutDuration"),
        "style_ms": _delta(loaded, before, "RecalcStyleDuration"),
        "network_bytes": initial_bytes,
        "document_transfer_bytes": navigation["transfer_size"],
        **shape,
        "long_tasks": len(all_long_tasks),
        "longest_task_ms": round(
            max((item["duration"] for item in all_long_tasks), default=0), 1,
        ),
        "poll_window_ms": settle_ms,
        "poll_bytes": encoded_bytes - initial_bytes,
        "poll_task_ms": _delta(poll_after, poll_before, "TaskDuration"),
        "poll_layout_ms": _delta(poll_after, poll_before, "LayoutDuration"),
        "poll_style_ms": _delta(poll_after, poll_before, "RecalcStyleDuration"),
        "poll_long_tasks": len(poll_long_tasks),
        "scroll_task_ms": _delta(scroll_after, scroll_before, "TaskDuration"),
        "scroll_layout_ms": _delta(scroll_after, scroll_before, "LayoutDuration"),
        "scroll_style_ms": _delta(scroll_after, scroll_before, "RecalcStyleDuration"),
    }
    page.close()
    return result


def _median_runs(runs: list[dict]) -> dict:
    out = {"runs": len(runs)}
    for key, value in runs[0].items():
        values = [run[key] for run in runs]
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            out[key] = round(statistics.median(values), 1)
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", action="append", required=True)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--poll-window-ms", type=int, default=7200)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()
    if args.runs < 1 or args.poll_window_ms < 0:
        parser.error("--runs must be positive and --poll-window-ms non-negative")

    report = {"schema_version": 1, "profiles": []}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=not args.headed)
        try:
            for url in args.url:
                runs = [
                    profile_once(browser, url, args.poll_window_ms)
                    for _ in range(args.runs)
                ]
                report["profiles"].append({
                    "url": url,
                    "median": _median_runs(runs),
                    "runs": runs,
                })
        finally:
            browser.close()

    output = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(output)
    else:
        sys.stdout.write(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
