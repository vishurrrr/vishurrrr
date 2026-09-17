#!/usr/bin/env python3
"""
Fetches real GitHub stats for GH_USERNAME and regenerates:
  - animated-lang-stats.svg   (language donuts, hourly commit bars, stats panel)
  - animated-contributions.svg (last-12-months contribution area chart)

Env vars:
  GH_USERNAME   GitHub username to report on (default: vishurrrr)
  GH_TOKEN      A token with public read access (Actions' built-in GITHUB_TOKEN is enough)

Run: python scripts/generate_profile_svgs.py
"""

import os
import re
import math
import datetime
from collections import Counter

import requests

USERNAME = os.environ.get("GH_USERNAME", "vishurrrr")
TOKEN = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
API = "https://api.github.com"
GRAPHQL_URL = "https://api.github.com/graphql"
TZ_OFFSET_HOURS = 5.5  # IST, matches the original "UTC +5.50" panel

HEADERS = {"Accept": "application/vnd.github+json"}
if TOKEN:
    HEADERS["Authorization"] = f"Bearer {TOKEN}"

LANG_COLORS = {
    "TypeScript": "#3178c6", "JavaScript": "#f1e05a", "C++": "#f34b7d",
    "HTML": "#e34c26", "C": "#8a8a8a", "Python": "#4584b6", "CSS": "#563d7c",
    "Jupyter Notebook": "#DA5B0B", "Shell": "#89e051", "Other": "#6e7681",
}
FALLBACK_COLOR = "#6e7681"


# ---------------------------------------------------------------- fetch ----

def gh_get(url, params=None):
    r = requests.get(url, headers=HEADERS, params=params, timeout=30)
    r.raise_for_status()
    return r


def paginate(url, params=None):
    items, page = [], 1
    params = dict(params or {})
    params["per_page"] = 100
    while True:
        params["page"] = page
        r = gh_get(url, params)
        batch = r.json()
        if not batch:
            break
        items.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return items


def commit_count(owner, repo, author):
    """Cheap total-commit-count trick: ask for 1 commit/page, read the 'last' page number."""
    r = requests.get(
        f"{API}/repos/{owner}/{repo}/commits",
        headers=HEADERS, params={"author": author, "per_page": 1}, timeout=30,
    )
    if r.status_code != 200:
        return 0
    link = r.headers.get("Link", "")
    m = re.search(r'[?&]page=(\d+)>; rel="last"', link)
    if m:
        return int(m.group(1))
    return len(r.json())


def search_total(query):
    return gh_get(f"{API}/search/issues", {"q": query, "per_page": 1}).json().get("total_count", 0)


def graphql(query, variables=None):
    r = requests.post(
        GRAPHQL_URL, headers=HEADERS,
        json={"query": query, "variables": variables or {}}, timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    if "errors" in data:
        raise RuntimeError(data["errors"])
    return data["data"]


def get_contributions_collection():
    q = """
    query($login: String!) {
      user(login: $login) {
        contributionsCollection {
          contributionCalendar { weeks { contributionDays { date contributionCount } } }
          repositoriesContributedTo(first: 1, contributionTypes: [COMMIT, ISSUE, PULL_REQUEST, REPOSITORY]) {
            totalCount
          }
        }
      }
    }"""
    return graphql(q, {"login": USERNAME})["user"]["contributionsCollection"]


def get_owned_repos():
    return [r for r in paginate(f"{API}/users/{USERNAME}/repos", {"type": "owner"}) if not r.get("fork")]


def collect_language_stats(repos):
    """
    repo_weighted: one vote per repo for its primary language -> 'Top Languages by Repo'
    commit_weighted: repo's language-byte mix, scaled by that repo's real commit count from
                      this user -> an approximation of 'Top Languages by Commit' (GitHub's API
                      doesn't expose per-language commit counts directly, so this weights each
                      repo's language footprint by how much the user actually committed there).
    """
    repo_weighted = Counter()
    commit_weighted = Counter()
    total_commits = 0
    hours = Counter({h: 0 for h in range(24)})

    for repo in repos:
        owner, name = repo["owner"]["login"], repo["name"]
        if repo.get("language"):
            repo_weighted[repo["language"]] += 1

        n_commits = commit_count(owner, name, USERNAME)
        total_commits += n_commits

        try:
            langs = gh_get(f"{API}/repos/{owner}/{name}/languages").json()
        except Exception:
            langs = {}
        total_bytes = sum(langs.values()) or 1
        for lang, b in langs.items():
            commit_weighted[lang] += (b / total_bytes) * n_commits

        # sample recent commit timestamps for the hourly histogram (cap per repo to stay light)
        try:
            commits = paginate(f"{API}/repos/{owner}/{name}/commits", {"author": USERNAME})
        except Exception:
            commits = []
        for c in commits[:200]:
            date_str = c["commit"]["author"]["date"]
            try:
                dt = datetime.datetime.strptime(date_str, "%Y-%m-%dT%H:%M:%SZ")
            except ValueError:
                continue
            local_hour = (dt + datetime.timedelta(hours=TZ_OFFSET_HOURS)).hour
            hours[local_hour] += 1

    return repo_weighted, commit_weighted, total_commits, [hours[h] for h in range(24)]


def gather_all_data():
    repos = get_owned_repos()
    repo_weighted, commit_weighted, total_commits, hourly = collect_language_stats(repos)
    total_stars = sum(r.get("stargazers_count", 0) for r in repos)
    total_prs = search_total(f"type:pr+author:{USERNAME}")
    total_issues = search_total(f"type:issue+author:{USERNAME}")
    contrib = get_contributions_collection()
    contributed_to = contrib["repositoriesContributedTo"]["totalCount"]

    # flatten the last-12-months daily calendar into ~13 monthly buckets for the area chart
    days = [d for w in contrib["contributionCalendar"]["weeks"] for d in w["contributionDays"]]
    monthly = Counter()
    order = []
    for d in days:
        key = d["date"][:7]  # YYYY-MM
        if key not in monthly:
            order.append(key)
        monthly[key] += d["contributionCount"]
    monthly_series = [(k, monthly[k]) for k in order]

    return dict(
        username=USERNAME,
        repo_weighted=repo_weighted,
        commit_weighted=commit_weighted,
        total_stars=total_stars,
        total_commits=total_commits,
        total_prs=total_prs,
        total_issues=total_issues,
        contributed_to=contributed_to,
        hourly=hourly,
        monthly_series=monthly_series,
    )


# --------------------------------------------------------------- helpers ---

def top_n_with_other(counter, n=5):
    items = counter.most_common()
    total = sum(v for _, v in items) or 1
    top = items[:n]
    rest = sum(v for _, v in items[n:])
    if rest > 0:
        top.append(("Other", rest))
    return [(name, val, val / total * 100) for name, val in top if val > 0]


def donut_segments(pairs, r=70):
    """pairs: list of (name, value, pct) -> segments with stroke-dasharray/offset for a CSS-drawn donut."""
    C = 2 * math.pi * r
    cum = 0
    segs = []
    for name, _, pct in pairs:
        length = C * pct / 100
        offset = -(cum / 100) * C
        segs.append(dict(name=name, pct=pct, length=length, offset=offset,
                          color=LANG_COLORS.get(name, FALLBACK_COLOR)))
        cum += pct
    return segs, C


def catmull_rom_path(points):
    d = "M {:.2f} {:.2f} ".format(*points[0])
    n = len(points)
    for i in range(n - 1):
        p0 = points[i - 1] if i > 0 else points[i]
        p1, p2 = points[i], points[i + 1]
        p3 = points[i + 2] if i + 2 < n else p2
        c1 = (p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6)
        c2 = (p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6)
        d += "C {:.2f} {:.2f} {:.2f} {:.2f} {:.2f} {:.2f} ".format(*c1, *c2, *p2)
    return d


# ------------------------------------------------------------- SVG build ---

def build_lang_stats_svg(data):
    repo_segs, C = donut_segments(top_n_with_other(data["repo_weighted"]))
    commit_pairs = [(k, v, v / (sum(data["commit_weighted"].values()) or 1) * 100)
                     for k, v in data["commit_weighted"].most_common()]
    commit_segs, _ = donut_segments(top_n_with_other(Counter(dict((k, v) for k, v, _ in commit_pairs))))

    hourly = data["hourly"]
    max_h = max(hourly) or 1
    bar_w, gap, base_y, chart_h = 13, 16, 420, 114
    bars_svg, x = [], 42
    for i, v in enumerate(hourly):
        h = max((v / max_h) * chart_h, 0.6)
        color = "#c53b53" if v == max_h else ("#bb9af7" if v >= max_h * 0.55 else "#7aa2f7")
        bars_svg.append(
            f'<rect class="bar" x="{x:.0f}" y="{base_y - h:.1f}" width="{bar_w}" height="{h:.1f}" '
            f'fill="{color}" style="animation-delay:{0.02 * i:.2f}s"/>'
        )
        x += gap

    def legend_rows(segs, x0):
        rows = []
        y = 70
        for i, s in enumerate(segs):
            rows.append(
                f'<g class="fade-in" style="animation-delay:{0.05 + 0.07 * i:.2f}s">'
                f'<rect class="swatch" x="{x0}" y="{y}" width="12" height="12" fill="{s["color"]}"/>'
                f'<text class="legend-text" x="{x0 + 20}" y="{y + 10}">{s["name"]}</text>'
                f'<text class="legend-pct" x="{x0 + 122}" y="{y + 10}">{s["pct"]:.0f}%</text></g>'
            )
            y += 25
        return "\n".join(rows)

    def donut_circles(segs, cx, cy, prefix):
        keyframes, circles = [], []
        for i, s in enumerate(segs):
            name = f"{prefix}{i}"
            keyframes.append(
                f"@keyframes {name} {{ from {{ stroke-dasharray: 0 {C:.2f}; }} "
                f"to {{ stroke-dasharray: {s['length']:.2f} {C:.2f}; }} }}"
            )
            circles.append(
                f'<circle class="seg" cx="{cx}" cy="{cy}" r="70" stroke="{s["color"]}" '
                f'stroke-dashoffset="{s["offset"]:.2f}" '
                f'style="animation: {name} 1s ease-out {0.3 + 0.12 * i:.2f}s forwards"/>'
            )
        return "\n".join(keyframes), "\n".join(circles)

    repo_kf, repo_circ = donut_circles(repo_segs, 335, 125, "gr")
    commit_kf, commit_circ = donut_circles(commit_segs, 805, 125, "gc")

    total_stars, total_commits = data["total_stars"], data["total_commits"]
    total_prs, total_issues = data["total_prs"], data["total_issues"]
    contributed_to = data["contributed_to"]

    return f"""<svg width="940" height="480" viewBox="0 0 940 480" xmlns="http://www.w3.org/2000/svg" font-family="'Segoe UI', Ubuntu, Helvetica, Arial, sans-serif">
  <defs>
    <style>
      .bg {{ fill: #1a1b27; }}
      .panel {{ fill: #1f2335; stroke: #292e42; stroke-width: 1; }}
      .title {{ fill: #7aa2f7; font-size: 17px; font-weight: 700; }}
      .legend-text {{ fill: #c0caf5; font-size: 13px; }}
      .legend-pct {{ fill: #565f89; font-size: 11px; }}
      .axis-text {{ fill: #565f89; font-size: 10px; }}
      .stat-label {{ fill: #9aa5ce; font-size: 13px; }}
      .stat-value {{ fill: #c0caf5; font-size: 16px; font-weight: 700; }}

      @keyframes fadeSlideUp {{ from {{ opacity: 0; transform: translateY(10px); }} to {{ opacity: 1; transform: translateY(0); }} }}
      .fade-in {{ opacity: 0; animation: fadeSlideUp 0.6s ease-out forwards; }}

      @keyframes growBar {{ from {{ transform: scaleY(0); }} to {{ transform: scaleY(1); }} }}
      .bar {{ transform-box: fill-box; transform-origin: bottom; animation: growBar 0.7s cubic-bezier(.34,1.56,.64,1) forwards; opacity: 0.95; }}

      @keyframes pulseDot {{ 0%, 100% {{ opacity: 0.4; r: 3; }} 50% {{ opacity: 1; r: 5; }} }}
      .pulse {{ animation: pulseDot 2.2s ease-in-out infinite; }}

      {repo_kf}
      {commit_kf}
      .seg {{ stroke-width: 22; fill: none; stroke-linecap: butt; }}
    </style>
  </defs>

  <rect class="bg" width="940" height="480" rx="10"/>

  <g>
    <rect class="panel" x="10" y="10" width="450" height="220" rx="10"/>
    <text class="title" x="28" y="40">Top Languages by Repo</text>
    {legend_rows(repo_segs, 28)}
    <g transform="rotate(-90 335 125)">
      <circle cx="335" cy="125" r="70" stroke="#292e42" stroke-width="22" fill="none"/>
      {repo_circ}
    </g>
  </g>

  <g>
    <rect class="panel" x="480" y="10" width="450" height="220" rx="10"/>
    <text class="title" x="498" y="40">Top Languages by Commit</text>
    {legend_rows(commit_segs, 498)}
    <g transform="rotate(-90 805 125)">
      <circle cx="805" cy="125" r="70" stroke="#292e42" stroke-width="22" fill="none"/>
      {commit_circ}
    </g>
  </g>

  <g>
    <rect class="panel" x="10" y="240" width="450" height="230" rx="10"/>
    <text class="title" x="28" y="270">Commits (UTC +5:30)</text>
    <line x1="40" y1="306" x2="440" y2="306" stroke="#292e42" stroke-width="1"/>
    <line x1="40" y1="363" x2="440" y2="363" stroke="#292e42" stroke-width="1"/>
    <text class="axis-text" x="18" y="310">{max_h}</text>
    <text class="axis-text" x="14" y="424">0</text>
    {"".join(bars_svg)}
    <text class="axis-text" x="40"  y="440">0</text>
    <text class="axis-text" x="190" y="440">6</text>
    <text class="axis-text" x="320" y="440">12</text>
    <text class="axis-text" x="382" y="440">18</text>
    <text class="axis-text" x="410" y="440">23</text>
    <text class="axis-text" x="230" y="458">per day hour</text>
  </g>

  <g>
    <rect class="panel" x="480" y="240" width="450" height="230" rx="10"/>
    <text class="title" x="498" y="270">Stats</text>
    <g class="fade-in" style="animation-delay:0.10s">
      <text x="498" y="305" font-size="15" fill="#e0af68">&#9733;</text>
      <text class="stat-label" x="520" y="305">Total Stars:</text>
      <text class="stat-value" x="680" y="305">{total_stars}</text>
    </g>
    <g class="fade-in" style="animation-delay:0.22s">
      <text class="stat-label" x="520" y="335">Total Commits:</text>
      <text class="stat-value" x="680" y="335">{total_commits}</text>
      <circle class="pulse" cx="712" cy="331" r="3" fill="#9ece6a"/>
    </g>
    <g class="fade-in" style="animation-delay:0.34s">
      <text class="stat-label" x="520" y="365">Total PRs:</text>
      <text class="stat-value" x="680" y="365">{total_prs}</text>
    </g>
    <g class="fade-in" style="animation-delay:0.46s">
      <text class="stat-label" x="520" y="395">Total Issues:</text>
      <text class="stat-value" x="680" y="395">{total_issues}</text>
    </g>
    <g class="fade-in" style="animation-delay:0.58s">
      <text class="stat-label" x="520" y="425">Contributed to:</text>
      <text class="stat-value" x="680" y="425">{contributed_to}</text>
    </g>
    <circle cx="880" cy="380" r="42" fill="#bb9af7" opacity="0.12" class="fade-in" style="animation-delay:0.15s"/>
    <circle cx="880" cy="380" r="30" fill="none" stroke="#bb9af7" stroke-width="2" class="fade-in" style="animation-delay:0.25s"/>
  </g>
</svg>
"""


def build_contrib_svg(data):
    series = data["monthly_series"][-13:] or [("", 0)]
    values = [v for _, v in series]
    maxv = max(values) or 1
    n = len(values)
    W, pad_left, pad_right = 1100, 40, 20
    H, pad_top = 220, 12
    xs = [pad_left + i * (W - pad_left - pad_right) / max(n - 1, 1) for i in range(n)]
    ys = [pad_top + (1 - v / maxv) * (H - pad_top - 4) for v in values]
    pts = list(zip(xs, ys))
    line_path = catmull_rom_path(pts)
    area_path = line_path + f"L {xs[-1]:.2f} {H:.2f} L {xs[0]:.2f} {H:.2f} Z"

    label_idx = list(range(0, n, max(n // 6, 1)))
    if label_idx[-1] != n - 1:
        label_idx.append(n - 1)
    labels_svg = "".join(
        f'<text class="axis-text fade-in" style="animation-delay:2.6s" x="{xs[i]-15:.0f}" y="248">{series[i][0]}</text>'
        for i in label_idx
    )

    last_x, last_y = xs[-1], ys[-1]

    return f"""<svg width="1160" height="300" viewBox="0 0 1160 300" xmlns="http://www.w3.org/2000/svg" font-family="'Segoe UI', Ubuntu, Helvetica, Arial, sans-serif">
  <defs>
    <linearGradient id="areaFill" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="#bb9af7" stop-opacity="0.55"/>
      <stop offset="100%" stop-color="#bb9af7" stop-opacity="0.03"/>
    </linearGradient>
    <style>
      .bg {{ fill: #1a1b27; }}
      .panel {{ fill: #1f2335; stroke: #292e42; stroke-width: 1; }}
      .title {{ fill: #7aa2f7; font-size: 16px; font-weight: 700; }}
      .sub {{ fill: #9ece6a; font-size: 12px; }}
      .axis-text {{ fill: #565f89; font-size: 11px; }}
      .grid {{ stroke: #292e42; stroke-width: 1; stroke-dasharray: 2 3; }}
      .line {{ fill: none; stroke: #bb9af7; stroke-width: 2.5; stroke-linecap: round; stroke-linejoin: round;
               stroke-dasharray: 1600; stroke-dashoffset: 1600; animation: drawLine 2.6s ease-out 0.3s forwards; }}
      @keyframes drawLine {{ to {{ stroke-dashoffset: 0; }} }}
      .area {{ fill: url(#areaFill); opacity: 0; animation: fadeArea 1.4s ease-out 1.6s forwards; }}
      @keyframes fadeArea {{ to {{ opacity: 1; }} }}
      @keyframes fadeIn {{ from {{ opacity: 0; }} to {{ opacity: 1; }} }}
      .fade-in {{ opacity: 0; animation: fadeIn 0.8s ease-out forwards; }}
      @keyframes pulse {{ 0%, 100% {{ r: 4; opacity: 0.9; }} 50% {{ r: 8; opacity: 0.15; }} }}
      .today-pulse {{ animation: pulse 1.8s ease-in-out infinite; animation-delay: 3s; }}
    </style>
  </defs>

  <rect class="bg" width="1160" height="300" rx="10"/>
  <rect class="panel" x="10" y="10" width="1140" height="280" rx="10"/>
  <text class="title" x="30" y="38">Contribution Activity</text>
  <text class="sub" x="900" y="38">contributions in the last year</text>

  <g transform="translate(0,30)">
    <path class="area" d="{area_path}"/>
    <path class="line" d="{line_path}"/>
    <g style="opacity:0" class="fade-in" style="animation-delay:2.9s">
      <circle class="today-pulse" cx="{last_x:.2f}" cy="{last_y:.2f}" fill="#9ece6a"/>
      <circle cx="{last_x:.2f}" cy="{last_y:.2f}" r="3.5" fill="#c0caf5"/>
    </g>
    {labels_svg}
  </g>
</svg>
"""


def main():
    if not TOKEN:
        print("Warning: no GH_TOKEN set — you'll hit low unauthenticated rate limits.")
    data = gather_all_data()
    with open("animated-lang-stats.svg", "w") as f:
        f.write(build_lang_stats_svg(data))
    with open("animated-contributions.svg", "w") as f:
        f.write(build_contrib_svg(data))
    print(f"Generated SVGs for {USERNAME}: "
          f"{data['total_commits']} commits, {data['total_stars']} stars, "
          f"{data['contributed_to']} repos contributed to.")


if __name__ == "__main__":
    main()
