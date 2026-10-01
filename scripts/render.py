#!/usr/bin/env python3
"""Render the profile README's SVG blocks from profile.yaml.

Usage:  python scripts/render.py            # writes assets/*.svg
        python scripts/render.py --offline  # skip the GitHub API, use fallback numbers

Fonts are embedded as base64 woff2 so the SVGs render identically everywhere
GitHub shows them (GitHub proxies images and blocks external font loads).
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import html
import io
import json
import os
import re
import sys
from functools import lru_cache
from pathlib import Path

import yaml
from fontTools.ttLib import TTFont
from fontTools.varLib import instancer

ROOT = Path(__file__).resolve().parent.parent
FONT_DIR = ROOT / "scripts" / "fonts"
OUT = ROOT / "assets"

# ---------------------------------------------------------------- palette
# D is the fixed dark palette used inside cards (cards are dark in both themes).
# C is the active palette for transparent blocks (hero text, headings, pills) and is
# swapped to LIGHT when rendering the *-light.svg variants.
D = {
    "ground": "#06080c",
    "surface": "#0c1016",
    "surface2": "#11171f",
    "line": "rgba(255,255,255,0.07)",
    "line_strong": "rgba(255,255,255,0.14)",
    "fg": "#e9eef4",
    "muted": "#8a95a3",
    "faint": "#59636f",
    "cyan": "#38bdf8",
    "violet": "#8b7cff",
    "green": "#34d399",
    "amber": "#f5b942",
    "soft": "#b9c3ce",
}
LIGHT = D | {
    "fg": "#1f2328",
    "muted": "#59636f",
    "faint": "#8a95a3",
    "line": "rgba(0,0,0,0.08)",
    "line_strong": "rgba(0,0,0,0.18)",
}
C = dict(D)


class theme:
    """`with theme("light"): ...` renders transparent blocks for a light page."""

    def __init__(self, name: str):
        self.pal = LIGHT if name == "light" else D

    def __enter__(self):
        C.clear()
        C.update(self.pal)

    def __exit__(self, *a):
        C.clear()
        C.update(D)
MONO = "'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"
SANS = "Manrope, ui-sans-serif, system-ui, -apple-system, 'Segoe UI', sans-serif"
W = 900  # design width of full-width blocks

# ---------------------------------------------------------------- fonts


class Face:
    """Measures text in a variable font at a given weight (no kerning; add slack)."""

    def __init__(self, path: Path):
        self.path = path
        self.base = TTFont(path)
        self.upem = self.base["head"].unitsPerEm

    @lru_cache(maxsize=None)
    def subset_b64(self, chars: str) -> str:
        """Base64 woff2 containing only `chars` (keeps the weight axis)."""
        from fontTools import subset

        f = TTFont(self.path)
        opts = subset.Options(flavor="woff2", layout_features=["kern", "liga", "calt"], notdef_outline=True)
        sub = subset.Subsetter(opts)
        sub.populate(text=chars + "n ")
        sub.subset(f)
        buf = io.BytesIO()
        f.flavor = "woff2"
        f.save(buf)
        return base64.b64encode(buf.getvalue()).decode()

    @lru_cache(maxsize=None)
    def _advances(self, weight: int) -> dict[int, int]:
        f = TTFont(self.path)
        if "fvar" in f:
            f = instancer.instantiateVariableFont(f, {"wght": weight}, inplace=True)
        cmap = f.getBestCmap()
        hmtx = f["hmtx"]
        return {cp: hmtx[g][0] for cp, g in cmap.items()}

    def width(self, text: str, size: float, weight: int = 400) -> float:
        adv = self._advances(weight)
        fallback = adv.get(ord("n"), self.upem // 2)
        return sum(adv.get(ord(ch), fallback) for ch in text) / self.upem * size

    def wrap(self, text: str, size: float, max_w: float, weight: int = 400) -> list[str]:
        words, lines, cur = text.split(), [], ""
        for w in words:
            cand = f"{cur} {w}".strip()
            if cur and self.width(cand, size, weight) > max_w:
                lines.append(cur)
                cur = w
            else:
                cur = cand
        if cur:
            lines.append(cur)
        return lines


MONO_FACE = Face(FONT_DIR / "JetBrainsMono-Latin.woff2")
SANS_FACE = Face(FONT_DIR / "Manrope-Latin.woff2")


def font_css(chars: str, mono=True, sans=True) -> str:
    css = []
    if mono:
        css.append(
            "@font-face{font-family:'JetBrains Mono';font-weight:100 800;font-display:block;"
            f"src:url(data:font/woff2;base64,{MONO_FACE.subset_b64(chars)}) format('woff2')}}"
        )
    if sans:
        css.append(
            "@font-face{font-family:Manrope;font-weight:200 800;font-display:block;"
            f"src:url(data:font/woff2;base64,{SANS_FACE.subset_b64(chars)}) format('woff2')}}"
        )
    return "".join(css)


# ---------------------------------------------------------------- svg helpers


def esc(s: str) -> str:
    return html.escape(str(s), quote=True)


def text(x, y, s, *, size, fill, family=SANS, weight=500, anchor="start", extra="", ls=None) -> str:
    ls_attr = f' letter-spacing="{ls}"' if ls else ""
    return (
        f'<text x="{x:.1f}" y="{y:.1f}" font-family="{family}" font-size="{size}" '
        f'font-weight="{weight}" fill="{fill}" text-anchor="{anchor}"{ls_attr} {extra}>{esc(s)}</text>'
    )


def tspans(x, y, parts, *, size, family=SANS, weight=500, extra="") -> str:
    """parts: list of (text, fill[, weight])."""
    inner = "".join(
        f'<tspan fill="{p[1]}"' + (f' font-weight="{p[2]}"' if len(p) > 2 else "") + f">{esc(p[0])}</tspan>"
        for p in parts
    )
    return f'<text x="{x:.1f}" y="{y:.1f}" font-family="{family}" font-size="{size}" font-weight="{weight}" {extra}>{inner}</text>'


def card(x, y, w, h, r=16, glow=False, tint=None, dark=False) -> str:
    """Dark card with a subtle top-down gradient, hairline border and optional gradient edge."""
    fill = f"url(#{tint})" if tint else "url(#cardfill)"
    line = D["line"] if dark else C["line"]
    s = f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{r}" fill="{fill}" stroke="{line}"/>'
    if glow:
        s += f'<rect x="{x+0.5}" y="{y+0.5}" width="{w-1}" height="{h-1}" rx="{r-0.5}" fill="none" stroke="url(#edge)" stroke-opacity="0.9"/>'
    return s


def defs() -> str:
    return (
        "<defs>"
        f'<linearGradient id="cardfill" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{C["surface2"]}"/><stop offset="1" stop-color="{C["surface"]}"/></linearGradient>'
        f'<linearGradient id="edge" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="{C["cyan"]}" stop-opacity="0.45"/><stop offset="0.4" stop-color="{C["cyan"]}" stop-opacity="0"/><stop offset="0.6" stop-color="{C["violet"]}" stop-opacity="0"/><stop offset="1" stop-color="{C["violet"]}" stop-opacity="0.4"/></linearGradient>'
        f'<linearGradient id="grad" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="{C["cyan"]}"/><stop offset="1" stop-color="{C["violet"]}"/></linearGradient>'
        f'<linearGradient id="feat" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="{C["cyan"]}" stop-opacity="0.16"/><stop offset="0.55" stop-color="{C["surface"]}"/></linearGradient>'
        f'<radialGradient id="glow1" cx="0.2" cy="0" r="0.7"><stop offset="0" stop-color="{C["cyan"]}" stop-opacity="0.16"/><stop offset="1" stop-color="{C["cyan"]}" stop-opacity="0"/></radialGradient>'
        f'<radialGradient id="glow2" cx="0.85" cy="0.1" r="0.6"><stop offset="0" stop-color="{C["violet"]}" stop-opacity="0.14"/><stop offset="1" stop-color="{C["violet"]}" stop-opacity="0"/></radialGradient>'
        "</defs>"
    )


def svg(w, h, body, *, css="", mono=True, sans=True, title="") -> str:
    used = "".join(sorted(set(html.unescape(re.sub(r"<[^>]+>", "", body)))))
    style = f"<style>{font_css(used, mono, sans)}{css}</style>"
    t = f"<title>{esc(title)}</title>" if title else ""
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" role="img">'
        f"{t}{style}{defs()}{body}</svg>"
    )


def eyebrow(x, y, cmd) -> str:
    return tspans(x, y, [("~ $ ", C["green"]), (cmd, C["muted"])], size=12, family=MONO, weight=500)


def write(name: str, content: str) -> None:
    OUT.mkdir(exist_ok=True)
    (OUT / name).write_text(content, encoding="utf-8")
    print(f"  wrote assets/{name} ({len(content)//1024} KB)")


# ---------------------------------------------------------------- data


def fetch_github(username: str, fallback: dict, offline: bool) -> dict:
    data = {
        "repos": fallback["repos"],
        "stars": fallback["stars"],
        "followers": fallback["followers"],
        "project_stars": dict(fallback["project_stars"]),
        "live": False,
    }
    if offline:
        return data
    try:
        import requests

        headers = {"Accept": "application/vnd.github+json"}
        tok = os.environ.get("GITHUB_TOKEN")
        if tok:
            headers["Authorization"] = f"Bearer {tok}"
        u = requests.get(f"https://api.github.com/users/{username}", headers=headers, timeout=15)
        u.raise_for_status()
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
        data.update(
            repos=u.json()["public_repos"],
            followers=u.json()["followers"],
            stars=sum(x["stargazers_count"] for x in repos),
            project_stars={x["name"]: x["stargazers_count"] for x in repos},
            live=True,
        )
    except Exception as e:  # noqa: BLE001 - any failure falls back to cached numbers
        print(f"  GitHub API unavailable ({e.__class__.__name__}); using fallback numbers", file=sys.stderr)
    return data


def fetch_contributions(username: str, offline: bool) -> dict | None:
    """Last-year contribution calendar from GitHub's public HTML (no token needed)."""
    if offline:
        return None
    try:
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
            days[date] = (int(level), count)
        if len(days) < 300:
            raise ValueError(f"only {len(days)} days parsed")
        return days
    except Exception as e:  # noqa: BLE001
        print(f"  contributions unavailable ({e.__class__.__name__}: {e}); keeping previous heatmap", file=sys.stderr)
        return None


# ---------------------------------------------------------------- blocks


def render_heatmap(days: dict) -> str:
    dates = sorted(days)
    first = dt.date.fromisoformat(dates[0])
    # align to the Sunday on/before the first date, like GitHub
    start = first - dt.timedelta(days=(first.weekday() + 1) % 7)
    last = dt.date.fromisoformat(dates[-1])
    weeks = (last - start).days // 7 + 1
    pad, top, cell, gap = 22, 58, 12, 4
    pitch = cell + gap
    gw = weeks * pitch - gap
    x0 = (W - gw) / 2 + 14
    h = top + 7 * pitch - gap + 46
    total = sum(c for _, c in days.values())
    levels = ["rgba(255,255,255,0.045)", "rgba(56,189,248,0.28)", "rgba(56,189,248,0.52)", "rgba(56,189,248,0.78)", "#7dd3fc"]
    body = [card(0, 0, W, h, 16, dark=True)]
    body.append(text(pad, 31, "Contributions", size=14, fill=D["fg"], weight=600))
    body.append(text(W - pad, 31, f"{total:,} in the last year", size=11.5, fill=D["faint"], family=MONO, weight=500, anchor="end"))
    # day labels
    for i, lbl in ((1, "Mon"), (3, "Wed"), (5, "Fri")):
        body.append(text(x0 - 10, top + i * pitch + cell - 2, lbl, size=10, fill=D["faint"], family=MONO, weight=400, anchor="end"))
    seen_month = None
    for w in range(weeks):
        for d in range(7):
            day = start + dt.timedelta(days=w * 7 + d)
            iso = day.isoformat()
            if iso not in days:
                continue
            lvl = days[iso][0]
            x, y = x0 + w * pitch, top + d * pitch
            body.append(f'<rect x="{x:.1f}" y="{y}" width="{cell}" height="{cell}" rx="2.5" fill="{levels[lvl]}"/>')
            if d == 0 and day.month != seen_month and day.day <= 7 and w < weeks - 1:
                seen_month = day.month
                body.append(text(x, top - 12, day.strftime("%b"), size=10, fill=D["faint"], family=MONO, weight=400))
    # legend
    ly = h - 20
    body.append(text(W - pad - 5 * (cell + 3) - 32, ly + 10, "less", size=10, fill=D["faint"], family=MONO, weight=400, anchor="end"))
    for i, col in enumerate(levels):
        body.append(f'<rect x="{W - pad - 5 * (cell + 3) - 26 + i * (cell + 3)}" y="{ly}" width="{cell}" height="{cell}" rx="2.5" fill="{col}"/>')
    body.append(text(W - pad, ly + 10, "more", size=10, fill=D["faint"], family=MONO, weight=400, anchor="end"))
    body.append(text(x0, ly + 10, f"{dates[0]} → {dates[-1]}", size=10, fill=D["faint"], family=MONO, weight=400))
    return svg(W, h, "".join(body), title="Contribution heatmap")



def render_hero(p: dict) -> str:
    """Transparent hero: eyebrow, name, lede on the left; dark identity panel on the right.
    Text colors follow the active theme (rendered twice, dark and light)."""
    h, pad = 276, 0
    rows = p["card"]["rows"]
    body = [tspans(pad, 34, [("~ $ ", C["green"]), (p["eyebrow"], C["muted"])], size=12, family=MONO, weight=500)]
    # name
    body.append(text(pad - 3, 106, p["name"]["first"], size=64, fill=C["fg"], weight=800, ls="-2.2"))
    body.append(text(pad - 3, 170, p["name"]["last"], size=64, fill="url(#grad)", weight=800, ls="-2.2"))
    last_w = SANS_FACE.width(p["name"]["last"], 64, 800) - 2.2 * len(p["name"]["last"])
    body.append(f'<rect class="caret" x="{pad+last_w+4:.0f}" y="120" width="4" height="50" rx="1" fill="{C["cyan"]}"/>')
    # lede
    lines = SANS_FACE.wrap(p["lede"], 14.5, 500, 500)
    bold = p.get("lede_bold", "")
    y = 210
    for ln in lines:
        if bold and ln.startswith(bold):
            body.append(tspans(pad, y, [(bold, C["fg"], 700), (ln[len(bold):], C["muted"])], size=14.5, weight=500))
        else:
            body.append(text(pad, y, ln, size=14.5, fill=C["muted"], weight=500))
        y += 22
    # identity panel (dark card in both themes)
    cw = 326
    ch = 56 + 25 * len(rows)
    cx, cy = W - cw, 8
    body.append(card(cx, cy, cw, ch, 18, dark=True))
    body.append(f'<circle cx="{cx+22}" cy="{cy+26}" r="3.5" fill="{D["green"]}"/>')
    body.append(text(cx + 32, cy + 30, p["card"]["title"].upper(), size=11, fill=D["faint"], family=MONO, weight=500, ls="0.8"))
    body.append(text(cx + cw - 20, cy + 30, p["card"]["version"], size=11, fill=D["faint"], family=MONO, weight=500, anchor="end"))
    body.append(f'<line x1="{cx+1}" y1="{cy+42}" x2="{cx+cw-1}" y2="{cy+42}" stroke="{D["line"]}"/>')
    key_w = max(MONO_FACE.width(r[0], 12.5, 400) for r in rows) + 18
    ry = cy + 68
    for r in rows:
        color = D.get(r[2], D["fg"]) if len(r) > 2 else D["fg"]
        body.append(text(cx + 22, ry, r[0], size=12.5, fill=D["faint"], family=MONO, weight=400))
        body.append(text(cx + 22 + key_w, ry, r[1], size=12.5, fill=color, family=MONO, weight=500))
        ry += 25
    css = (
        ".caret{animation:blink 1.1s steps(1) infinite}@keyframes blink{50%{opacity:0}}"
        "@media (prefers-reduced-motion:reduce){.caret{animation:none}}"
    )
    return svg(W, h, "".join(body), css=css, title=f"{p['name']['first']} {p['name']['last']}")


ICONS = {
    "site": "M12 2a10 10 0 100 20 10 10 0 000-20zm6.9 9h-3a15 15 0 00-1.3-5.4A8 8 0 0118.9 11zM12 4c1 1.3 1.8 3.7 2 7h-4c.2-3.3 1-5.7 2-7zM4.1 13h3a15 15 0 001.3 5.4A8 8 0 014.1 13zm3-2h-3a8 8 0 014.3-5.4A15 15 0 007.1 11zM12 20c-1-1.3-1.8-3.7-2-7h4c-.2 3.3-1 5.7-2 7zm2.6-1.6A15 15 0 0015.9 13h3a8 8 0 01-4.3 5.4z",
    "linkedin": "M4.98 3.5A2.5 2.5 0 112.5 6a2.48 2.48 0 012.48-2.5zM3 8.5h4V21H3zM9.5 8.5h3.8v1.7h.1a4.2 4.2 0 013.8-2c4 0 4.8 2.7 4.8 6.1V21h-4v-5.9c0-1.4 0-3.2-2-3.2s-2.3 1.5-2.3 3.1V21h-4z",
    "x": "M18.2 2h3.4l-7.4 8.5L23 22h-6.8l-5.3-7-6.1 7H1.4l7.9-9.1L1 2h7l4.8 6.4zm-1.2 18h1.9L7.1 3.9H5.1z",
    "medium": "M7 5.5A6.5 6.5 0 117 18.5 6.5 6.5 0 017 5.5zm10 .5c1.8 0 3.3 2.7 3.3 6s-1.5 6-3.3 6-3.3-2.7-3.3-6 1.5-6 3.3-6zm5.3.7c.6 0 1.2 2.4 1.2 5.3s-.5 5.3-1.2 5.3S21 14.9 21 12s.6-5.3 1.3-5.3z",
    "instagram": "M12 7a5 5 0 100 10 5 5 0 000-10zm0 8.2a3.2 3.2 0 110-6.4 3.2 3.2 0 010 6.4zM17.3 5.5a1.2 1.2 0 100 2.4 1.2 1.2 0 000-2.4zM21.9 7.9c-.1-1.7-.5-3.2-1.7-4.4S17.6 1.9 15.9 1.8C14.2 1.7 9.8 1.7 8.1 1.8 6.4 1.9 4.9 2.3 3.7 3.5S2 6.2 1.9 7.9c-.1 1.7-.1 6.1 0 7.8.1 1.7.5 3.2 1.7 4.4s2.7 1.6 4.4 1.7c1.7.1 6.1.1 7.8 0 1.7-.1 3.2-.5 4.4-1.7s1.6-2.7 1.7-4.4c.1-1.7.1-6.1 0-7.8zm-2.2 9.6a3.2 3.2 0 01-1.8 1.8c-1.3.5-4.3.4-5.9.4s-4.6.1-5.9-.4a3.2 3.2 0 01-1.8-1.8c-.5-1.3-.4-4.3-.4-5.9s-.1-4.6.4-5.9A3.2 3.2 0 016.1 4c1.3-.5 4.3-.4 5.9-.4s4.6-.1 5.9.4a3.2 3.2 0 011.8 1.8c.5 1.3.4 4.3.4 5.9s.1 4.6-.4 5.9z",
}


def render_link(link: dict) -> tuple[str, int]:
    label, primary = link["label"], link.get("primary", False)
    size = 12.5
    tw = MONO_FACE.width(label, size, 500)
    w = int(14 + 14 + 8 + tw + 16)
    h = 36
    light = C["fg"] == LIGHT["fg"]
    fill = C["fg"] if primary else ("rgba(0,0,0,0.03)" if light else "rgba(255,255,255,0.025)")
    stroke = C["fg"] if primary else C["line_strong"]
    fg = ("#ffffff" if light else "#0a0d12") if primary else C["fg"]
    body = [
        f'<rect x="0.5" y="0.5" width="{w-1}" height="{h-1}" rx="{(h-1)/2}" fill="{fill}" stroke="{stroke}"/>',
        f'<g transform="translate(14,11) scale(0.58)"><path d="{ICONS[link["id"]]}" fill="{fg}" fill-opacity="0.9"/></g>',
        text(14 + 14 + 8, 22.5, label, size=size, fill=fg, family=MONO, weight=500),
    ]
    return svg(w, h, "".join(body), sans=False, title=label), w


def render_stats(p: dict, gh: dict) -> str:
    tiles = p["stats"]["tiles"]
    gap, h = 14, 118
    tw = (W - gap * (len(tiles) - 1)) / len(tiles)
    years = dt.date.today().year - p["stats"]["first_commit_year"]
    values = {"repos": gh["repos"], "stars": gh["stars"], "followers": gh["followers"], "years": years}
    body = []
    for i, t in enumerate(tiles):
        x = i * (tw + gap)
        body.append(card(x, 0, tw, h, 16))
        body.append(text(x + 22, 27, t["label"].upper(), size=11, fill=C["faint"], family=MONO, weight=500, ls="0.9"))
        if t.get("live") and gh["live"]:
            body.append(text(x + tw - 22, 27, "LIVE", size=10.5, fill=C["green"], family=MONO, weight=600, anchor="end", ls="1"))
        val = f"{values[t['key']]:,}"
        body.append(text(x + 20, 70, val, size=38, fill=C["fg"], weight=800, ls="-1.2"))
        if t.get("suffix"):
            vx = x + 20 + SANS_FACE.width(val, 38, 800) - 1.2 * len(val) + 6
            body.append(text(vx, 70, t["suffix"], size=15, fill=C["muted"], weight=600))
        body.append(text(x + 22, 95, t["sub"], size=12.5, fill=C["muted"], weight=500))
    return svg(W, h, "".join(body), title="GitHub stats")


def render_section(cmd: str, title: str) -> str:
    h = 78
    body = [eyebrow(0, 32, cmd), text(-1, 66, title, size=26, fill=C["fg"], weight=700, ls="-0.7")]
    return svg(W, h, "".join(body), title=title)


def render_project(pr: dict, stars: int, colors: dict) -> str:
    w, h = 290, 176
    name = pr.get("name", pr["repo"])
    body = [card(0, 0, w, h, 16, glow=(pr is not None and pr.get("feature", False)))]
    body.append(tspans(20, 32, [("~/", C["faint"], 400), (name, C["fg"], 600)], size=14.5, family=MONO, weight=600))
    sw = MONO_FACE.width(str(stars), 12, 500)
    body.append(text(w - 20, 32, str(stars), size=12, fill=C["muted"], family=MONO, weight=500, anchor="end"))
    body.append(
        f'<g transform="translate({w-20-sw-17:.1f},21) scale(0.5)"><path d="M12 2l3 6.6 7 .8-5.2 4.9 1.4 7.1L12 18l-6.2 3.4 1.4-7.1L2 9.4l7-.8z" fill="{C["amber"]}"/></g>'
    )
    y = 60
    for ln in SANS_FACE.wrap(pr["desc"], 13, w - 40, 500)[:3]:
        body.append(text(20, y, ln, size=13, fill=C["muted"], weight=500))
        y += 19
    # tags
    tx, ty = 20, h - 40
    lang = pr["lang"]
    tags = [(lang, colors.get(lang))] + [(t, None) for t in pr["tags"]]
    for label, dot in tags:
        tw = MONO_FACE.width(label, 11, 500) + 16 + (12 if dot else 0)
        if tx + tw > w - 20:
            break
        body.append(
            f'<rect x="{tx}" y="{ty}" width="{tw:.0f}" height="22" rx="6" fill="rgba(255,255,255,0.04)" stroke="{C["line"]}"/>'
        )
        lx = tx + 8
        if dot:
            body.append(f'<circle cx="{lx+3.5}" cy="{ty+11}" r="3.5" fill="{dot}"/>')
            lx += 12
        body.append(text(lx, ty + 15, label, size=11, fill=C["muted"], family=MONO, weight=500))
        tx += tw + 6
    return svg(w, h, "".join(body), title=name)


def render_stack(p: dict, colors: dict) -> str:
    groups = p["stack"]
    gap = 14
    gw = (W - gap * (len(groups) - 1)) / len(groups)
    # layout chips first to know the height
    laid, max_h = [], 0
    for g in groups:
        chips, x, y = [], 20, 52
        for item in g["items"]:
            dot = colors.get(item)
            cw = MONO_FACE.width(item, 12.5, 500) + 22 + (13 if dot else 0)
            if x + cw > gw - 20 and x > 20:
                x, y = 20, y + 36
            chips.append((item, x, y, cw, dot))
            x += cw + 8
        laid.append(chips)
        max_h = max(max_h, y + 30 + 20)
    h = int(max_h)
    body = []
    for i, (g, chips) in enumerate(zip(groups, laid)):
        gx = i * (gw + gap)
        body.append(card(gx, 0, gw, h, 16))
        body.append(text(gx + 22, 30, f"[{g['title']}]", size=11.5, fill=C["faint"], family=MONO, weight=500, ls="0.9"))
        body.append(text(gx + gw - 22, 30, str(len(g["items"])), size=11.5, fill=C["muted"], family=MONO, weight=500, anchor="end"))
        for item, x, y, cw, dot in chips:
            body.append(
                f'<rect x="{gx+x:.1f}" y="{y}" width="{cw:.0f}" height="30" rx="8" fill="rgba(255,255,255,0.025)" stroke="{C["line"]}"/>'
            )
            lx = gx + x + 11
            if dot:
                body.append(f'<circle cx="{lx+3.5:.1f}" cy="{y+15}" r="3.5" fill="{dot}"/>')
                lx += 13
            body.append(text(lx, y + 19.5, item, size=12.5, fill=C["fg"], family=MONO, weight=500))
    return svg(W, h, "".join(body), title="Stack")


def render_quote(p: dict) -> str:
    q = p["quote"]
    lines = SANS_FACE.wrap("“" + q["text"] + "”", 22, W - 120, 600)
    h = 44 + 30 * len(lines) + 48
    body = [card(0, 0, W, h, 18)]
    y = 58
    for ln in lines:
        body.append(text(W / 2, y, ln, size=22, fill="#d5dde6", weight=600, anchor="middle", ls="-0.4"))
        y += 30
    body.append(text(W / 2, y + 8, q["cite"], size=12, fill=C["faint"], family=MONO, weight=400, anchor="middle"))
    return svg(W, h, "".join(body), title="Quote")


def render_statusbar(p: dict) -> str:
    h = 40
    today = dt.date.today().isoformat()
    body = [
        f'<line x1="0" y1="0.5" x2="{W}" y2="0.5" stroke="{C["line"]}"/>',
        tspans(0, 27, [("main", C["muted"], 500), (" ✓    utf-8    zsh", C["faint"])], size=12, family=MONO, weight=400),
        tspans(W, 27, [("updated ", C["faint"]), (today, C["muted"], 500), (f"    {p['username']}", C["faint"])], size=12, family=MONO, weight=400, extra='text-anchor="end"'),
    ]
    return svg(W, h, "".join(body), sans=False, title="Status")


# ---------------------------------------------------------------- readme


def render_readme(p: dict, link_widths: dict[str, int]) -> str:
    u = p["username"]
    S = p["sections"]
    def pic(name: str, alt: str, attr: str) -> str:
        return (
            f'<picture><source media="(prefers-color-scheme: dark)" srcset="assets/{name}.svg">'
            f'<img src="assets/{name}-light.svg" {attr} alt="{esc(alt)}"></picture>'
        )

    links = "\n".join(f'  <a href="{l["url"]}">{pic("link-" + l["id"], l["label"], "height=\"36\"")}</a>' for l in p["links"])
    projects = "\n".join(
        f'  <a href="https://github.com/{u}/{pr["repo"]}"><img src="assets/project-{pr["repo"]}.svg" width="32.6%" alt="{esc(pr.get("name", pr["repo"]))}"></a>'
        for pr in p["projects"]
    )
    more = "\n".join(
        f"| [{name}](https://github.com/{u}/{name}) | {desc} | `{stack}` |" for name, desc, stack in p["more_projects"]
    )
    theme = "bg_color=0c1016&title_color=e9eef4&text_color=8a95a3&icon_color=38bdf8&border_color=1b222c&hide_border=false&border_radius=16"
    return f"""<!-- Rendered by scripts/render.py from profile.yaml. Edit those, not this file. -->

<p>
  <a href="https://satheesh.dev">{pic("hero", "Satheesh Kumar, Staff Software Engineer at HackerEarth", 'width="100%"')}</a>
</p>

<p>
{links}
</p>

<img src="assets/stats.svg" width="100%" alt="Repositories, stars, followers and years shipping">

{pic("section-activity", "Activity", 'width="100%"')}

<p>
  <a href="https://github.com/{u}"><img src="https://github-readme-stats.vercel.app/api?username={u}&show_icons=true&include_all_commits=true&count_private=true&{theme}&ring_color=38bdf8" width="49.5%" alt="GitHub stats"></a>
  <a href="https://github.com/{u}"><img src="https://github-readme-streak-stats.herokuapp.com/?user={u}&background=0c1016&border=1b222c&stroke=1b222c&ring=38bdf8&fire=f5b942&currStreakNum=e9eef4&sideNums=e9eef4&currStreakLabel=38bdf8&sideLabels=8a95a3&dates=59636f&border_radius=16" width="49.5%" alt="GitHub streak"></a>
</p>

<a href="https://github.com/{u}"><img src="assets/heatmap.svg" width="100%" alt="Contribution heatmap, last 12 months"></a>

{pic("section-projects", "Featured projects", 'width="100%"')}

<p>
{projects}
</p>

<details>
<summary><b>More things I've built</b></summary>
<br>

| Project | What it is | Stack |
|---|---|---|
{more}

</details>

{pic("section-stack", "Stack", 'width="100%"')}

<img src="assets/stack.svg" width="100%" alt="Languages, frameworks, data and infra">

<br>

<img src="assets/quote.svg" width="100%" alt="{esc(p["quote"]["text"])}">

{pic("statusbar", "", 'width="100%"')}
"""


# ---------------------------------------------------------------- main


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true", help="skip the GitHub API")
    ap.add_argument("--no-readme", action="store_true", help="render SVGs only")
    args = ap.parse_args()

    p = yaml.safe_load((ROOT / "profile.yaml").read_text(encoding="utf-8"))
    print("Fetching GitHub data...")
    gh = fetch_github(p["username"], p["fallback"], args.offline)
    print(f"  repos={gh['repos']} stars={gh['stars']} followers={gh['followers']} live={gh['live']}")

    print("Rendering...")
    link_widths = {}
    for variant in ("dark", "light"):
        suffix = "" if variant == "dark" else "-light"
        with theme(variant):
            write(f"hero{suffix}.svg", render_hero(p))
            for l in p["links"]:
                s, w = render_link(l)
                link_widths[l["id"]] = w
                write(f"link-{l['id']}{suffix}.svg", s)
            for key, sec in p["sections"].items():
                write(f"section-{key}{suffix}.svg", render_section(sec["cmd"], sec["title"]))
            write(f"statusbar{suffix}.svg", render_statusbar(p))
    write("stats.svg", render_stats(p, gh))
    days = fetch_contributions(p["username"], args.offline)
    if days:
        write("heatmap.svg", render_heatmap(days))
    for pr in p["projects"]:
        write(f"project-{pr['repo']}.svg", render_project(pr, gh["project_stars"].get(pr["repo"], 0), p["lang_colors"]))
    write("stack.svg", render_stack(p, p["lang_colors"]))
    write("quote.svg", render_quote(p))
    if not args.no_readme:
        (ROOT / "README.md").write_text(render_readme(p, link_widths), encoding="utf-8")
        print("  wrote README.md")
    (ROOT / "assets" / "data.json").write_text(json.dumps({k: v for k, v in gh.items() if k != "project_stars"} | {"rendered": dt.date.today().isoformat()}, indent=2))


if __name__ == "__main__":
    main()
