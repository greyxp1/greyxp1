#!/usr/bin/env python3
"""Generate clean SVG stats cards for GitHub profile README."""

import json
import os
import time
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from html import escape
from http.client import HTTPResponse
from pathlib import Path
from typing import TypeAlias, cast

TOKEN = os.environ["METRICS_TOKEN"]
USER = os.environ["GITHUB_ACTOR"]
OUTPUT = Path(os.environ.get("GENERATED_DIR", "."))
TIMEOUT = 30
MAX_WORKERS = 8

JsonValue: TypeAlias = (
    bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"] | None
)
JsonObject: TypeAlias = dict[str, JsonValue]


def gh(url: str, accept: str = "application/vnd.github.v3+json") -> JsonValue:
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"token {TOKEN}",
            "User-Agent": "profile-stats",
            "Accept": accept,
        },
    )
    with cast(HTTPResponse, urllib.request.urlopen(req, timeout=TIMEOUT)) as response:
        return cast(JsonValue, json.loads(response.read()))


def gh_search(url: str) -> JsonObject:
    return cast(JsonObject, gh(url, accept="application/vnd.github.cloak-preview"))


def get_all_repos() -> list[JsonObject]:
    repos: list[JsonObject] = []
    page = 1
    while True:
        batch = cast(
            list[JsonObject],
            gh(
                f"https://api.github.com/users/{USER}/repos?per_page=100&page={page}&sort=updated"
            ),
        )
        if not batch:
            break
        repos.extend(batch)
        page += 1
    return repos


def repo_str(repo: JsonObject, key: str) -> str:
    return cast(str, repo[key])


def get_repo_languages(repo: JsonObject) -> dict[str, int]:
    try:
        data = cast(JsonObject, gh(repo_str(repo, "languages_url")))
        return {lang: cast(int, bytes_) for lang, bytes_ in data.items()}
    except Exception as e:
        print(f"  Warning: could not fetch languages for {repo_str(repo, 'name')}: {e}")
        return {}


def get_repo_line_delta(repo: JsonObject) -> tuple[int, int]:
    retries = 5
    for attempt in range(retries):
        try:
            data = gh(repo_str(repo, "url") + "/stats/code_frequency")
            if not isinstance(data, list):
                if attempt + 1 < retries:
                    wait = 2 ** attempt
                    print(
                        f"  Retrying {repo_str(repo, 'name')} in {wait}s (attempt {attempt + 1})..."
                    )
                    time.sleep(wait)
                    continue
                return (0, 0)
            weeks = cast(list[list[int]], data)
            return (
                sum(week[1] for week in weeks if week[1] > 0),
                sum(abs(week[2]) for week in weeks if week[2] < 0),
            )
        except Exception as e:
            if attempt + 1 < retries:
                wait = 2 ** attempt
                print(
                    f"  Retrying {repo_str(repo, 'name')} in {wait}s (attempt {attempt + 1}): {e}"
                )
                time.sleep(wait)
                continue
            print(
                f"  Warning: could not fetch code frequency for {repo_str(repo, 'name')}: {e}"
            )
            return (0, 0)
    return (0, 0)


OCTICONS = {
    "commits": '<svg class="octicon" viewBox="0 0 16 16" width="16" height="16"><path fill-rule="evenodd" d="M10.5 7.75a2.5 2.5 0 11-5 0 2.5 2.5 0 015 0zm1.43.75a4.002 4.002 0 01-7.86 0H.75a.75.75 0 110-1.5h3.32a4.001 4.001 0 017.86 0h3.32a.75.75 0 110 1.5h-3.32z"/></svg>',
    "prs": '<svg class="octicon" viewBox="0 0 16 16" width="16" height="16"><path fill-rule="evenodd" d="M7.177 3.073L9.573.677A.25.25 0 0110 .854v4.792a.25.25 0 01-.427.177L7.177 3.427a.25.25 0 010-.354zM3.75 2.5a.75.75 0 100 1.5.75.75 0 000-1.5zm-2.25.75a2.25 2.25 0 113 2.122v5.256a2.251 2.251 0 11-1.5 0V5.372A2.25 2.25 0 011.5 3.25zM11 2.5h-1V4h1a1 1 0 011 1v5.628a2.251 2.251 0 101.5 0V5A2.5 2.5 0 0011 2.5zm1 10.25a.75.75 0 111.5 0 .75.75 0 01-1.5 0zM3.75 12a.75.75 0 100 1.5.75.75 0 000-1.5z"/></svg>',
    "issues": '<svg class="octicon" viewBox="0 0 16 16" width="16" height="16"><path d="M8 9.5a1.5 1.5 0 100-3 1.5 1.5 0 000 3z"/><path fill-rule="evenodd" d="M8 0a8 8 0 100 16A8 8 0 008 0zM1.5 8a6.5 6.5 0 1113 0 6.5 6.5 0 01-13 0z"/></svg>',
    "lines": '<svg class="octicon" viewBox="0 0 16 16" width="16" height="16"><path fill-rule="evenodd" d="M2.5 1.75a.25.25 0 01.25-.25h8.5a.25.25 0 01.25.25v7.736a.75.75 0 101.5 0V1.75A1.75 1.75 0 0011.25 0h-8.5A1.75 1.75 0 001 1.75v11.5c0 .966.784 1.75 1.75 1.75h3.17a.75.75 0 000-1.5H2.75a.25.25 0 01-.25-.25V1.75zM4.75 4a.75.75 0 000 1.5h4.5a.75.75 0 000-1.5h-4.5zM4 7.75A.75.75 0 014.75 7h2a.75.75 0 010 1.5h-2A.75.75 0 014 7.75zm11.774 3.537a.75.75 0 00-1.048-1.074L10.7 14.145 9.281 12.72a.75.75 0 00-1.062 1.058l1.943 1.95a.75.75 0 001.055.008l4.557-4.45z"/></svg>',
    "repos": '<svg class="octicon" viewBox="0 0 16 16" width="16" height="16"><path fill-rule="evenodd" d="M2 2.5A2.5 2.5 0 014.5 0h8.75a.75.75 0 01.75.75v12.5a.75.75 0 01-.75.75h-2.5a.75.75 0 110-1.5h1.75v-2h-8a1 1 0 00-.714 1.7.75.75 0 01-1.072 1.05A2.495 2.495 0 012 11.5v-9zm10.5-1V9h-8c-.356 0-.694.074-1 .208V2.5a1 1 0 011-1h8zM5 12.25v3.25a.25.25 0 00.4.2l1.45-1.087a.25.25 0 01.3 0L8.6 15.7a.25.25 0 00.4-.2v-3.25a.25.25 0 00-.25-.25h-3.5a.25.25 0 00-.25.25z"/></svg>',
}

LANG_COLORS = {
    "Nix": "#7e7eff",
    "JavaScript": "#f1e05a",
    "TypeScript": "#3178c6",
    "CSS": "#663399",
    "Rust": "#dea584",
    "Shell": "#89e051",
    "HTML": "#e34c26",
    "Python": "#3572A5",
    "Go": "#00ADD8",
    "Java": "#b07219",
    "C": "#555555",
    "C++": "#f34b7d",
    "Ruby": "#701516",
    "Lua": "#000080",
    "Kotlin": "#A97BFF",
}

CARD_CSS = """<style>
svg {
  font-family: -apple-system, BlinkMacSystemFont, Segoe UI, Helvetica, Arial, sans-serif, Apple Color Emoji, Segoe UI Emoji;
  font-size: 14px;
  line-height: 21px;
}
#background {
  width: calc(100% - 10px);
  height: calc(100% - 10px);
  fill: #00000000;
  stroke: #8B8B8B22;
  stroke-width: 1px;
  rx: 6px;
  ry: 6px;
}
foreignObject {
  width: calc(100% - 42px);
}
.octicon {
  margin-right: 1ch;
  vertical-align: top;
}
</style>"""


def fmt(n: int) -> str:
    return f"{n:,}"


def card(body: str, y: int, height: int) -> str:
    return f"""<svg width="360" height="210" xmlns="http://www.w3.org/2000/svg">
{CARD_CSS}
<rect x="5" y="5" id="background" />
<foreignObject x="21" y="{y}" width="318" height="{height}">
{body}
</foreignObject>
</svg>"""


def render_overview(stats: tuple[str, int, int, int, int, int]) -> str:
    name, commits, prs, issues, lines, repos = stats

    items = [
        ("commits", "Commits", fmt(commits)),
        ("lines", "Lines of code changed", fmt(lines)),
        ("prs", "Pull requests", fmt(prs)),
        ("issues", "Issues opened", fmt(issues)),
        ("repos", "Repositories contributed", fmt(repos)),
    ]
    rows_html = "".join(
        f"""
<tr><td class="label">{OCTICONS[key]}{label}</td><td class="value">{value}</td></tr>"""
        for key, label, value in items
    )

    return card(
        f"""<style>
foreignObject {{ height: calc(100% - 38px); }}
table {{
  width: 100%;
  border-collapse: collapse;
}}
th {{
  padding: 0 0.5em 0.5em;
  text-align: left;
  font-size: 14px;
  font-weight: 600;
  color: #ffffff;
}}
td {{
  padding: 0.25em;
  font-size: 12px;
  line-height: 18px;
  color: rgb(145, 145, 145);
}}
.label {{
  font-weight: 600;
  color: rgb(139, 139, 139);
}}
.value {{
  text-align: right;
  font-weight: 400;
}}
.octicon {{ fill: rgb(139, 139, 139); }}
</style>
<div xmlns="http://www.w3.org/1999/xhtml">
<table>
<thead><tr>
<th colspan="2">{escape(name)}&#x2019;s GitHub Statistics</th>
</tr></thead>
<tbody>{rows_html}
</tbody>
</table>
</div>""",
        19,
        172,
    )


def render_languages(languages: dict[str, int]) -> str:
    total = max(1, sum(languages.values()))
    lang_rows = [
        (lang, bytes_ / total * 100, LANG_COLORS.get(lang, "#8b8b8b"))
        for lang, bytes_ in sorted(languages.items(), key=lambda x: -x[1])
    ]
    progress_html = "".join(
        f'<span style="background-color:{color};width:{pct:.3f}%;margin-right:{pct * 0.01:.3f}%"></span>'
        for _, pct, color in lang_rows
    )
    lang_list_html = "".join(
        f"""
<li>
<svg xmlns="http://www.w3.org/2000/svg" class="octicon" style="fill:{color}" viewBox="0 0 16 16" width="16" height="16"><path fill-rule="evenodd" d="M8 4a4 4 0 100 8 4 4 0 000-8z"></path></svg>
<span class="lang">{escape(lang)}</span>
<span class="percent">{pct:.2f}%</span>
</li>"""
        for lang, pct, color in lang_rows[:10]
    )

    return card(
        f"""<style>
foreignObject {{ height: calc(100% - 34px); }}
h2 {{
  margin: 0 0 0.75em;
  line-height: 24px;
  font-size: 16px;
  font-weight: 600;
  color: #ffffff;
}}
ul {{
  list-style: none;
  padding-left: 0;
  margin: 0;
}}
li {{
  display: inline-flex;
  font-size: 12px;
  margin-right: 2ch;
  align-items: center;
  flex-wrap: nowrap;
}}
div.ellipsis {{
  height: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
}}
.progress {{
  display: flex;
  height: 8px;
  overflow: hidden;
  background-color: #00000000;
  border-radius: 6px;
  outline: 1px solid transparent;
  margin-bottom: 1em;
}}
.lang {{
  font-weight: 600;
  margin-right: 4px;
  color: rgb(135, 135, 135);
}}
.percent {{ color: rgb(150,150,150); }}
</style>
<div xmlns="http://www.w3.org/1999/xhtml" class="ellipsis">
<h2>Most Used Languages</h2>
<div><span class="progress">{progress_html}</span></div>
<ul>{lang_list_html}
</ul>
</div>""",
        17,
        176,
    )


def main() -> None:
    print("Fetching user info...")
    user = cast(JsonObject, gh(f"https://api.github.com/users/{USER}"))
    name = cast(str | None, user.get("name")) or USER
    print(f"  Name: {name}")

    print("Fetching commit count...")
    commits_data = gh_search(f"https://api.github.com/search/commits?q=author:{USER}")
    total_commits = cast(int, commits_data["total_count"])
    print(f"  Commits: {total_commits}")

    print("Fetching PR count...")
    prs_data = gh_search(
        f"https://api.github.com/search/issues?q=author:{USER}+type:pr"
    )
    total_prs = cast(int, prs_data["total_count"])
    print(f"  PRs: {total_prs}")

    print("Fetching issue count...")
    issues_data = gh_search(
        f"https://api.github.com/search/issues?q=author:{USER}+type:issue"
    )
    total_issues = cast(int, issues_data["total_count"])
    print(f"  Issues: {total_issues}")

    print("Fetching repos...")
    repos = get_all_repos()
    print(f"  Repos: {len(repos)}")

    print("Fetching languages...")
    languages: defaultdict[str, int] = defaultdict(int)
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        for lang_data in executor.map(get_repo_languages, repos):
            for lang, bytes_ in lang_data.items():
                languages[lang] += bytes_

    print("Computing lines changed...")
    total_added = 0
    total_deleted = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        for added, deleted in executor.map(get_repo_line_delta, repos):
            total_added += added
            total_deleted += deleted

    lines_changed = total_added + total_deleted
    print(f"  Lines added: {total_added:,}")
    print(f"  Lines deleted: {total_deleted:,}")
    print(f"  Total lines changed: {lines_changed:,}")

    print("Generating overview SVG...")
    svg = render_overview(
        (name, total_commits, total_prs, total_issues, lines_changed, len(repos))
    )
    overview_path = OUTPUT / "generated.overview.svg"
    _ = overview_path.write_text(svg, encoding="utf-8")
    print(f"  Written to {overview_path}")

    print("Generating languages SVG...")
    svg_langs = render_languages(dict(languages))
    langs_path = OUTPUT / "generated.languages.svg"
    _ = langs_path.write_text(svg_langs, encoding="utf-8")
    print(f"  Written to {langs_path}")

    print("Done!")


if __name__ == "__main__":
    main()
