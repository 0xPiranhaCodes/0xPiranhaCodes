#!/usr/bin/env python3
"""Render the profile README and its SVG blocks from profile.yaml.

Usage:  python scripts/render.py            # writes README.md and assets/*.svg
        python scripts/render.py --offline  # skip GitHub, reuse assets/data.json

The fonts are embedded as base64 woff2 so the SVGs render identically everywhere
GitHub shows them (GitHub proxies images and blocks external font loads).
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import html
import io
import json
import os
import re
import sys
import textwrap
from functools import lru_cache
from pathlib import Path

import yaml
from fontTools import subset
from fontTools.ttLib import TTFont

ROOT = Path(__file__).resolve().parent.parent
FONT = ROOT / "scripts" / "fonts" / "JetBrainsMono-Latin.woff2"
OUT = ROOT / "assets"
DATA = OUT / "data.json"

# ---------------------------------------------------------------- palette
# GitHub's own design tokens, read from the live profile page, on a transparent
# background so the blocks sit in the page. `levels` are its contribution colours.
PALETTES = {
    "dark": {
        "ink": "#f0f6fc",  # fgColor-default
        "muted": "#9198a1",  # fgColor-muted
        "accent": "#4493f8",  # fgColor-accent
        "green": "#3fb950",  # fgColor-success
        "border": "#3d444d",  # borderColor-default
        "border_accent": "#1f6feb",  # borderColor-accent-emphasis
        "surface": "#151b23",  # bgColor-muted
        "levels": ["#151b23", "#033a16", "#196c2e", "#2ea043", "#56d364"],
        "glow": True,
    },
    "light": {
        "ink": "#1f2328",
        "muted": "#59636e",
        "accent": "#0969da",
        "green": "#1a7f37",
        "border": "#d1d9e0",
        "border_accent": "#0969da",
        "surface": "#f6f8fa",
        "levels": ["#eff2f5", "#aceebb", "#4ac26b", "#2da44e", "#116329"],
        "glow": False,
    },
}
MONO = "'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"

# ---------------------------------------------------------------- svg helpers


def esc(s: str) -> str:
    return html.escape(str(s), quote=True)


@lru_cache(maxsize=None)
def subset_b64(chars: str) -> str:
    """Base64 woff2 containing only `chars` (keeps the weight axis)."""
    f = TTFont(FONT)
    opts = subset.Options(flavor="woff2", layout_features=["kern"], notdef_outline=True)
    sub = subset.Subsetter(opts)
    sub.populate(text=chars + "n ")
    sub.subset(f)
    buf = io.BytesIO()
    f.flavor = "woff2"
    f.save(buf)
    return base64.b64encode(buf.getvalue()).decode()


def text(x, y, s, *, size, fill, weight=500, anchor="start") -> str:
    return (
        f'<text x="{x:.1f}" y="{y:.1f}" font-family="{MONO}" font-size="{size}" '
        f'font-weight="{weight}" fill="{fill}" text-anchor="{anchor}">{esc(s)}</text>'
    )


def svg(w, h, body, *, title, css="") -> str:
    used = "".join(sorted(set(html.unescape(re.sub(r"<[^>]+>", "", body)))))
    font = (
        "@font-face{font-family:'JetBrains Mono';font-weight:100 800;font-display:block;"
        f"src:url(data:font/woff2;base64,{subset_b64(used)}) format('woff2')}}"
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" role="img">'
        f"<title>{esc(title)}</title><style>{font}{css}</style>{body}</svg>"
    )


def write(name: str, content: str) -> None:
    OUT.mkdir(exist_ok=True)
    (OUT / name).write_text(content, encoding="utf-8")
    print(f"  wrote assets/{name} ({len(content)//1024} KB)")


# ---------------------------------------------------------------- data


def fetch_stars(username: str) -> dict[str, int]:
    import requests

    headers = {"Accept": "application/vnd.github+json"}
    tok = os.environ.get("GITHUB_TOKEN")
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    repos, page = [], 1
    while True:
        r = requests.get(
            f"https://api.github.com/users/{username}/repos",
            params={"per_page": 100, "page": page, "type": "owner"},
            headers=headers,
            timeout=15,
        )
        r.raise_for_status()
        chunk = r.json()
        repos += chunk
        if len(chunk) < 100:
            break
        page += 1
    return {x["name"]: x["stargazers_count"] for x in repos}


def fetch_contributions(username: str) -> dict[str, list[int]]:
    """Last-year contribution calendar from GitHub's public HTML (no token needed)."""
    import requests

    r = requests.get(f"https://github.com/users/{username}/contributions", timeout=20,
                     headers={"User-Agent": "profile-readme-renderer"})
    r.raise_for_status()
    h = r.text
    cells = re.findall(r'<td[^>]*data-date="(\d{4}-\d{2}-\d{2})"[^>]*id="([^"]+)"[^>]*data-level="(\d)"', h)
    tips = dict(re.findall(r'<tool-tip[^>]*for="([^"]+)"[^>]*>([^<]*)</tool-tip>', h))
    days = {}
    for date, cid, level in cells:
        m = re.match(r"(\d+|No) contribution", tips.get(cid, ""))
        count = 0 if not m or m.group(1) == "No" else int(m.group(1))
        days[date] = [int(level), count]
    if len(days) < 300:
        raise ValueError(f"only {len(days)} days parsed")
    return days


def load_data(username: str, offline: bool) -> dict:
    """Live numbers, falling back to the last render's for whatever GitHub won't give us."""
    data = json.loads(DATA.read_text(encoding="utf-8"))
    if offline:
        return data
    for key, fetch in (("stars", fetch_stars), ("days", fetch_contributions)):
        try:
            data[key] = fetch(username)
        except Exception as e:  # noqa: BLE001 - any failure keeps the cached numbers
            print(f"  {key} unavailable ({e.__class__.__name__}: {e}); keeping previous", file=sys.stderr)
    return data


# ---------------------------------------------------------------- blocks


def poly(points, fill: str, attr: str = "") -> str:
    pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
    return f'<polygon points="{pts}" fill="{fill}"{attr}/>'


def render_hero(days: dict, pal: dict) -> str:
    """The real contribution graph in perspective: every active day rises off the floor."""
    dates = sorted(days)
    first = dt.date.fromisoformat(dates[0])
    # align to the Sunday on/before the first date, like GitHub
    start = first - dt.timedelta(days=(first.weekday() + 1) % 7)
    weeks = (dt.date.fromisoformat(dates[-1]) - start).days // 7 + 1

    pitch, inset, tilt = 17, 2 / 17, 0.3
    w = weeks * pitch - 4
    near, depth = 216, 150  # screen y of the nearest row, and the floor's screen height
    h = near + 42
    rise = 12  # height per contribution level
    vanish = near - depth / (1 - 1 / (1 + tilt))

    def pt(u: float, v: float, z: float = 0) -> tuple[float, float]:
        """Project week u, weekday v, height z. Row 0 is furthest away."""
        s = 1 / (1 + tilt * (7 - v) / 7)
        return w / 2 + (u - weeks / 2) * pitch * s, vanish + (near - vanish) * s - z * s

    body = []
    b = 16  # corner brackets
    for x, y, dx, dy in ((0.5, 0.5, b, b), (w - 0.5, 0.5, -b, b), (0.5, h - 0.5, b, -b), (w - 0.5, h - 0.5, -b, -b)):
        body.append(f'<path d="M{x} {y + dy}V{y}H{x + dx}" fill="none" stroke="{pal["border"]}" stroke-width="1.5"/>')

    # Far to near: nearer bars paint over the ones behind them.
    bars = []
    for row in range(7):
        for col in range(weeks):
            iso = (start + dt.timedelta(days=col * 7 + row)).isoformat()
            if iso not in days:
                continue
            a, c = col + inset, row + inset
            e, f = col + 1 - inset, row + 1 - inset
            level = days[iso][0]
            if not level:
                body.append(poly([pt(a, c), pt(e, c), pt(e, f), pt(a, f)], pal["levels"][0]))
                continue
            z = level * rise
            bars.append(poly([pt(a, f), pt(e, f), pt(e, f, z), pt(a, f, z)], pal["levels"][level - 1]))
            bars.append(poly([pt(a, c, z), pt(e, c, z), pt(e, f, z), pt(a, f, z)], pal["levels"][level]))

    seen_month = None
    for col in range(weeks):
        day = start + dt.timedelta(days=col * 7)
        if day.month != seen_month and day.day <= 7 and col < weeks - 1:
            seen_month = day.month
            x, y = pt(col, 7)
            body.append(f'<rect x="{x:.1f}" y="{y + 7:.1f}" width="1" height="7" fill="{pal["muted"]}"/>')
            body.append(text(x + 6, y + 18, day.strftime("%b"), size=12, fill=pal["muted"], weight=400))

    glow = ' filter="url(#glow)"' if pal["glow"] else ""
    body.append(f"<g{glow}>{''.join(bars)}</g>")

    # The scanner: one strip per week, each flaring in turn, invisible in a frozen frame.
    for col in range(weeks):
        strip = [pt(col, 0, 4 * rise), pt(col + 1, 0, 4 * rise), pt(col + 1, 7), pt(col, 7)]
        body.append(poly(strip, pal["green"], f' class="scan" opacity="0" style="animation-delay:{col * 0.04:.2f}s"'))

    defs = (
        '<defs><filter id="glow" x="-5%" y="-30%" width="110%" height="160%"><feGaussianBlur stdDeviation="3"/>'
        '<feMerge><feMergeNode/><feMergeNode in="SourceGraphic"/></feMerge></filter></defs>'
    )
    css = (
        ".scan{animation:scan 7s linear infinite}"
        "@keyframes scan{0%{opacity:0}2%{opacity:.22}9%,100%{opacity:0}}"
        "@media (prefers-reduced-motion:reduce){.scan{animation:none}}"
    )
    return svg(w, h, defs + "".join(body), title="Contribution graph", css=css)


def render_prompt(cmd: str, pal: dict, cursor=False) -> str:
    """Chevron, command, and a rail of light; or the idle prompt that ends the session."""
    size, w, h = 15, 640, 30
    cw = size * 0.6  # JetBrains Mono advances 600/1000
    tx = 22
    body = (
        f'<path d="M3 9l6 6-6 6" fill="none" stroke="{pal["accent"]}" stroke-width="2.2"/>'
        f'<path d="M10 9l6 6-6 6" fill="none" stroke="{pal["green"]}" stroke-width="2.2"/>'
        + text(tx, 20, cmd, size=size, fill=pal["ink"])
    )
    x = tx + len(cmd) * cw
    css = ""
    if cursor:
        w = int(x + 2 * cw)
        body += f'<rect class="cursor" x="{x:.1f}" y="7" width="{cw:.1f}" height="17" fill="{pal["green"]}"/>'
        css = (
            ".cursor{animation:blink 1.1s steps(1) infinite}@keyframes blink{50%{opacity:0}}"
            "@media (prefers-reduced-motion:reduce){.cursor{animation:none}}"
        )
    else:
        rail = x + 14
        body = (
            f'<defs><linearGradient id="rail"><stop offset="0" stop-color="{pal["border"]}"/>'
            f'<stop offset="1" stop-color="{pal["border"]}" stop-opacity="0"/></linearGradient></defs>'
            f'{body}<rect x="{rail:.1f}" y="14.5" width="{w - rail:.1f}" height="1" fill="url(#rail)"/>'
        )
    return svg(w, h, body, title=cmd or "End of session", css=css)


def chamfer(w: float, h: float, cut: int) -> str:
    """The one shape in the system: a box with two clipped corners."""
    return f"M{cut} .5H{w - .5}V{h - cut}L{w - cut} {h - .5}H.5V{cut}Z"


def panel(w: float, h: float, pal: dict, *, strong=False, dashed=False) -> str:
    fill, stroke = (pal["surface"], pal["border_accent"]) if strong else ("none", pal["border"])
    dash = ' stroke-dasharray="6 5"' if dashed else ""
    return f'<path d="{chamfer(w, h, 14)}" fill="{fill}" stroke="{stroke}"{dash}/>'


def render_chip(label: str, pal: dict) -> str:
    """A link as a chamfered HUD chip."""
    size, h = 13.5, 34
    w = int(len(label) * size * 0.6 + 40)
    body = (
        f'<path d="{chamfer(w, h, 9)}" fill="{pal["surface"]}" stroke="{pal["border"]}"/>'
        f'<rect x="14" y="14" width="6" height="6" fill="{pal["green"]}"/>'
        + text(28, 22, label, size=size, fill=pal["ink"], weight=600)
    )
    return svg(w, h, body, title=label)


CARD_W = 400  # at natural size two sit side by side, and they stack on a phone
FACT_W = 37  # characters of value that fit beside the key column


def fact_lines(p: dict) -> list[tuple[str, str]]:
    lines = []
    for key, value in p["facts"]:
        for i, ln in enumerate(textwrap.wrap(value, FACT_W)):
            lines.append((key if i == 0 else "", ln))
    return lines


def panel_height(p: dict) -> int:
    return 36 + 23 * len(fact_lines(p))


def render_statement(p: dict, pal: dict) -> str:
    h = panel_height(p)
    body = [panel(CARD_W, h, pal, strong=True)]
    y = 54
    for ln in textwrap.wrap(p["statement"], 21):
        body.append(text(22, y, ln, size=28, fill=pal["ink"], weight=800))
        y += 37
    y += 8
    for ln in textwrap.wrap(p["about"], 42):
        body.append(text(22, y, ln, size=13, fill=pal["muted"]))
        y += 20
    assert y - 20 < h - 8, "statement overflows its panel"
    return svg(CARD_W, h, "".join(body), title=p["statement"])


def render_facts(p: dict, pal: dict) -> str:
    body = [panel(CARD_W, panel_height(p), pal)]
    y = 34
    for key, value in fact_lines(p):
        if key:
            body.append(text(22, y, key, size=12.5, fill=pal["muted"], weight=600))
        body.append(text(102, y, value, size=12.5, fill=pal["ink"]))
        y += 23
    return svg(CARD_W, panel_height(p), "".join(body), title="Profile facts")


def project_lines(pr: dict) -> list[str]:
    return textwrap.wrap(" ".join(pr["desc"]), 48)


def render_project(p: dict, pr: dict, stars: int, pal: dict) -> str:
    rows = max(len(project_lines(x)) for x in p["projects"])
    h = 88 + rows * 18
    meta = " · ".join(m for m in (pr["lang"], pr.get("tag", "")) if m)
    name = pr.get("name", pr["repo"])
    soon = pr.get("soon", False)
    body = [
        panel(CARD_W, h, pal, strong=pr is p["projects"][0], dashed=soon),
        text(22, 36, name, size=18, fill=pal["muted" if soon else "accent"], weight=800),
        text(22, 56, meta, size=12, fill=pal["muted"]),
    ]
    if stars:
        x = CARD_W - 22
        body.append(text(x, 35, str(stars), size=13, fill=pal["ink"], weight=700, anchor="end"))
        sx = x - len(str(stars)) * 7.8 - 19
        body.append(f'<g transform="translate({sx:.1f},23) scale(0.6)"><path d="M12 2l3 6.6 7 .8-5.2 4.9 1.4 7.1L12 18l-6.2 3.4 1.4-7.1L2 9.4l7-.8z" fill="{pal["muted"]}"/></g>')
    y = 82
    for ln in project_lines(pr):
        body.append(text(22, y, ln, size=12.5, fill=pal["ink"], weight=400))
        y += 18
    return svg(CARD_W, h, "".join(body), title=name)


def render_timeline(p: dict, pal: dict) -> str:
    stops = p["timeline"]
    w, h, rail = 840, 118, 46
    step = (w - 120) / (len(stops) - 1)
    body = [f'<rect x="0" y="{rail - 1}" width="{w}" height="2" fill="{pal["border"]}"/>']
    for i, (year, title_, note) in enumerate(stops):
        x = 60 + i * step
        last = i == len(stops) - 1
        fill = pal["green"] if last else pal["accent"]
        body.append(text(x, 26, str(year), size=16, fill=pal["ink"], weight=800, anchor="middle"))
        body.append(f'<rect x="{x - 6:.1f}" y="{rail - 6}" width="12" height="12" fill="{fill}" transform="rotate(45 {x:.1f} {rail})"/>')
        body.append(text(x, 76, title_, size=12, fill=fill, weight=700, anchor="middle"))
        body.append(text(x, 94, note, size=11.5, fill=pal["muted"], weight=400, anchor="middle"))
    return svg(w, h, "".join(body), title="Career timeline, 2017 to 2026")


# ---------------------------------------------------------------- readme


def render_readme(p: dict) -> str:
    u = p["username"]

    def url(name: str) -> str:
        # GitHub caches branch assets by path, so the hash makes a new render show up at once.
        digest = hashlib.sha256((OUT / f"{name}.svg").read_bytes()).hexdigest()[:8]
        return f"assets/{name}.svg?v={digest}"

    def pic(name: str, alt: str, attr: str = "") -> str:
        return (
            f'<picture><source media="(prefers-color-scheme: dark)" srcset="{url(name)}">'
            f'<img src="{url(name + "-light")}"{attr} alt="{esc(alt)}"></picture>'
        )

    def shell(key: str) -> str:
        return pic(f"prompt-{key}", f"$ {p['prompts'][key]}")

    def fold(label: str, inner: str) -> str:
        return f"<details>\n<summary><samp>{esc(label)}</samp></summary>\n<br>\n\n{inner}\n\n</details>"

    links = "\n".join(f'  <a href="{l["url"]}">{pic("chip-" + l["name"], l["name"])}</a>' for l in p["links"])
    def card(pr: dict) -> str:
        image = pic("project-" + pr["repo"], pr.get("name", pr["repo"]))
        return f"  {image}" if pr.get("soon") else f'  <a href="https://github.com/{u}/{pr["repo"]}">{image}</a>'

    cards = "\n".join(card(pr) for pr in p["projects"])
    folds = "\n\n".join((
        fold("cat ~/career.log", f"```text\n{p['career'].rstrip()}\n```"),
        fold("git log --oneline webpty", f"```text\n{p['webpty_log'].rstrip()}\n```"),
        fold("cat stack.toml", f"```toml\n{p['stack'].rstrip()}\n```"),
    ))
    return f"""<!-- Rendered by scripts/render.py from profile.yaml. Edit those, not this file. -->

<p>
  <a href="https://github.com/{u}?tab=repositories">{pic("hero", "Contribution graph of the last 12 months, with active days rising as green bars", ' width="100%"')}</a>
</p>

<p>
{links}
</p>

{shell("whoami")}

<p>
  {pic("statement", p["statement"])}
  {pic("facts", "; ".join(f"{k}: {v}" for k, v in p["facts"]))}
</p>

{shell("projects")}

<p>
{cards}
</p>

{shell("career")}

{pic("timeline", "; ".join(f"{y} {t}, {n}" for y, t, n in p["timeline"]), ' width="100%"')}

{folds}

{shell("history")}

```diff
{p["history"].rstrip()}
```

{pic("prompt-end", "End of session")}

<sub><samp>{esc(p["footer"])}</samp></sub>
"""


# ---------------------------------------------------------------- main


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true", help="skip GitHub, reuse assets/data.json")
    ap.add_argument("--no-readme", action="store_true", help="render SVGs only")
    args = ap.parse_args()

    p = yaml.safe_load((ROOT / "profile.yaml").read_text(encoding="utf-8"))
    print("Fetching GitHub data...")
    data = load_data(p["username"], args.offline)

    print("Rendering...")
    for variant, pal in PALETTES.items():
        suffix = "" if variant == "dark" else "-light"
        write(f"hero{suffix}.svg", render_hero(data["days"], pal))
        for key, cmd in p["prompts"].items():
            write(f"prompt-{key}{suffix}.svg", render_prompt(cmd, pal))
        write(f"statement{suffix}.svg", render_statement(p, pal))
        write(f"facts{suffix}.svg", render_facts(p, pal))
        write(f"timeline{suffix}.svg", render_timeline(p, pal))
        for pr in p["projects"]:
            stars = data["stars"].get(pr["repo"], 0)
            write(f"project-{pr['repo']}{suffix}.svg", render_project(p, pr, stars, pal))
        write(f"prompt-end{suffix}.svg", render_prompt("", pal, cursor=True))
        for l in p["links"]:
            write(f"chip-{l['name']}{suffix}.svg", render_chip(l["name"], pal))
    if not args.no_readme:
        (ROOT / "README.md").write_text(render_readme(p), encoding="utf-8")
        print("  wrote README.md")
    DATA.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")


if __name__ == "__main__":
    main()
