"""Generate clean SVG stats cards for GitHub profile README."""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
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


class SearchChangedError(RuntimeError):
    """GitHub's search index changed while collecting commits."""


def gh(url: str, data: JsonObject | None = None) -> JsonValue:
    req = urllib.request.Request(
        url,
        data=json.dumps(data).encode() if data is not None else None,
        headers={
            "Authorization": f"token {TOKEN}",
            "User-Agent": "profile-stats",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
        },
    )
    for attempt in range(3):
        try:
            with cast(
                HTTPResponse, urllib.request.urlopen(req, timeout=TIMEOUT)
            ) as response:
                result = cast(JsonValue, json.loads(response.read()))
            break
        except urllib.error.HTTPError as error:
            message = error.read().decode("utf-8", errors="replace")
            retry_after = error.headers.get("Retry-After")
            primary_limit = error.headers.get("X-RateLimit-Remaining") == "0"
            secondary_limit = "secondary rate limit" in message.lower()
            if (
                attempt == 2
                or error.code not in (403, 429)
                or not (
                    retry_after or primary_limit or secondary_limit or error.code == 429
                )
            ):
                raise RuntimeError(
                    f"GitHub API error {error.code}: {message}"
                ) from error
            if retry_after:
                wait = max(1, float(retry_after))
            elif primary_limit:
                wait = max(1, int(error.headers["X-RateLimit-Reset"]) - time.time() + 1)
            else:
                wait = 60 * 2**attempt
            print(f"  Waiting {wait:.0f}s for GitHub's rate limit...")
            time.sleep(wait)
    if isinstance(result, dict) and result.get("errors"):
        raise RuntimeError(f"GitHub API errors: {result['errors']}")
    return result


def search_commits(query: str, page: int = 1, order: str = "asc") -> JsonObject:
    params = urllib.parse.urlencode(
        {
            "q": query,
            "per_page": 100,
            "page": page,
            "sort": "committer-date",
            "order": order,
        }
    )
    for attempt in range(3):
        # Pace searches below 30 requests per minute and back off on incomplete results.
        time.sleep(2.1 if attempt == 0 else 5 * 2**attempt)
        result = cast(JsonObject, gh(f"https://api.github.com/search/commits?{params}"))
        if not result["incomplete_results"]:
            return result
        print(f"  Incomplete commit search (attempt {attempt + 1}/3)")
    raise RuntimeError("GitHub returned an incomplete commit search after 3 attempts")


def find_commits(query: str, date_range: str | None = None) -> list[JsonObject]:
    search = f"{query} committer-date:{date_range}" if date_range else query
    result = search_commits(search)
    count = cast(int, result["total_count"])
    print(f"  Found {count} commits: {search}")
    commits = cast(list[JsonObject], result["items"])
    if count > 1000:
        latest_result = search_commits(search, order="desc")
        if latest_result["total_count"] != count:
            raise SearchChangedError("Commit search count changed while finding dates")
        latest = cast(list[JsonObject], latest_result["items"])

        def committed_at(item: JsonObject) -> datetime:
            commit = cast(JsonObject, item["commit"])
            committer = cast(JsonObject, commit["committer"])
            return datetime.fromisoformat(cast(str, committer["date"])).astimezone(UTC)

        first, last = committed_at(commits[0]), committed_at(latest[0])
        midpoint = (first + (last - first) / 2).replace(microsecond=0)
        if midpoint >= last or midpoint < first:
            raise RuntimeError(
                "Cannot split commit search below GitHub's 1,000-result limit"
            )

        def timestamp(value: datetime) -> str:
            return value.strftime("%Y-%m-%dT%H:%M:%SZ")

        commits = find_commits(query, f"{timestamp(first)}..{timestamp(midpoint)}")
        commits += find_commits(
            query, f"{timestamp(midpoint + timedelta(seconds=1))}..{timestamp(last)}"
        )
    else:
        for page in range(2, (count + 99) // 100 + 1):
            batch = search_commits(search, page)
            if batch["total_count"] != count:
                raise SearchChangedError(
                    "Commit search count changed during pagination"
                )
            commits.extend(cast(list[JsonObject], batch["items"]))
    pairs = {
        (cast(int, cast(JsonObject, item["repository"])["id"]), repo_str(item, "sha"))
        for item in commits
    }
    if len(commits) != count or len(pairs) != count:
        raise SearchChangedError(
            "Commit search returned inconsistent or overlapping results"
        )
    return commits


def get_authored_stats() -> tuple[int, int, int, int]:
    for attempt in range(3):
        try:
            matches = find_commits(f"author:{USER}")
            break
        except SearchChangedError as error:
            if attempt == 2:
                raise
            wait = 10 * 2**attempt
            print(
                f"  {error}; restarting commit search in {wait}s (attempt {attempt + 2}/3)"
            )
            time.sleep(wait)
    repositories = {
        repo_str(cast(JsonObject, item["repository"]), "full_name") for item in matches
    }
    commits = list({repo_str(item, "sha"): item for item in matches}.values())
    added = deleted = 0
    for start in range(0, len(commits), 100):
        batch = commits[start : start + 100]
        result = cast(
            JsonObject,
            gh(
                "https://api.github.com/graphql",
                {
                    "query": """
                query($ids: [ID!]!) {
                    nodes(ids: $ids) {
                        ... on Commit {
                            oid additions deletions
                            parents { totalCount }
                        }
                    }
                }
            """,
                    "variables": {"ids": [item["node_id"] for item in batch]},
                },
            ),
        )
        data = cast(JsonObject, result["data"])
        nodes = cast(list[JsonObject | None], data["nodes"])
        for item, node in zip(batch, nodes, strict=True):
            if node is None or node.get("oid") != item["sha"]:
                raise RuntimeError(f"Could not read commit {item['sha']}")
            parents = cast(JsonObject, node["parents"])
            if cast(int, parents["totalCount"]) <= 1:
                added += cast(int, node["additions"])
                deleted += cast(int, node["deletions"])
        print(f"  Read {start + len(batch)}/{len(commits)} unique commits")
    return len(commits), len(repositories), added, deleted


def get_overview() -> tuple[int, int, int, int, int, int]:
    result = cast(
        JsonObject,
        gh(
            "https://api.github.com/graphql",
            {
                "query": """
            query($user: String!) {
                user(login: $user) {
                    pullRequests(states: [MERGED]) { totalCount }
                    issues { totalCount }
                }
            }
        """,
                "variables": {
                    "user": USER,
                },
            },
        ),
    )
    data = cast(JsonObject, result["data"])
    user = cast(JsonObject, data["user"])
    prs = cast(JsonObject, user["pullRequests"])
    issues = cast(JsonObject, user["issues"])
    commits, repositories, added, deleted = get_authored_stats()
    return (
        commits,
        cast(int, prs["totalCount"]),
        cast(int, issues["totalCount"]),
        repositories,
        added,
        deleted,
    )


def get_all_repos() -> list[JsonObject]:
    repos: list[JsonObject] = []
    page = 1
    while True:
        batch = cast(
            list[JsonObject],
            gh(
                f"https://api.github.com/users/{USER}/repos?type=owner&per_page=100&page={page}"
            ),
        )
        if not batch:
            break
        repos.extend(repo for repo in batch if not repo["fork"])
        page += 1
    return repos


def repo_str(repo: JsonObject, key: str) -> str:
    return cast(str, repo[key])


def get_repo_languages(repo: JsonObject) -> dict[str, int]:
    data = cast(JsonObject, gh(repo_str(repo, "languages_url")))
    return {lang: cast(int, bytes_) for lang, bytes_ in data.items()}


OCTICONS = {
    "commits": '<svg class="octicon" viewBox="0 0 16 16" width="16" height="16"><path fill-rule="evenodd" d="M10.5 7.75a2.5 2.5 0 11-5 0 2.5 2.5 0 015 0zm1.43.75a4.002 4.002 0 01-7.86 0H.75a.75.75 0 110-1.5h3.32a4.001 4.001 0 017.86 0h3.32a.75.75 0 110 1.5h-3.32z"/></svg>',
    "prs": '<svg class="octicon" viewBox="0 0 16 16" width="16" height="16"><path fill-rule="evenodd" d="M7.177 3.073L9.573.677A.25.25 0 0110 .854v4.792a.25.25 0 01-.427.177L7.177 3.427a.25.25 0 010-.354zM3.75 2.5a.75.75 0 100 1.5.75.75 0 000-1.5zm-2.25.75a2.25 2.25 0 113 2.122v5.256a2.251 2.251 0 11-1.5 0V5.372A2.25 2.25 0 011.5 3.25zM11 2.5h-1V4h1a1 1 0 011 1v5.628a2.251 2.251 0 101.5 0V5A2.5 2.5 0 0011 2.5zm1 10.25a.75.75 0 111.5 0 .75.75 0 01-1.5 0zM3.75 12a.75.75 0 100 1.5.75.75 0 000-1.5z"/></svg>',
    "issues": '<svg class="octicon" viewBox="0 0 16 16" width="16" height="16"><path d="M8 9.5a1.5 1.5 0 100-3 1.5 1.5 0 000 3z"/><path fill-rule="evenodd" d="M8 0a8 8 0 100 16A8 8 0 008 0zM1.5 8a6.5 6.5 0 1113 0 6.5 6.5 0 01-13 0z"/></svg>',
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
  height: calc(100% - 34px);
}
h2 {
  margin: 0 0 0.75em;
  line-height: 24px;
  font-size: 16px;
  font-weight: 600;
  color: #ffffff;
}
.octicon {
  margin-right: 1ch;
  vertical-align: top;
}
</style>"""


def fmt(n: int) -> str:
    return f"{n:,}"


def card(body: str) -> str:
    return f"""<svg width="360" height="240" xmlns="http://www.w3.org/2000/svg">
{CARD_CSS}
<rect x="5" y="5" id="background" />
<foreignObject x="21" y="17" width="318" height="206">
{body}
</foreignObject>
</svg>"""


def render_overview(stats: tuple[int, int, int, int, int, int]) -> str:
    commits, prs, issues, repos, added, deleted = stats

    items = [
        (
            "commits",
            "Lines changed",
            (
                f'<span class="additions">+{fmt(added)}</span> '
                f'<span class="deletions">−{fmt(deleted)}</span>'
            ),
        ),
        ("commits", "Commits authored", fmt(commits)),
        ("repos", "Repositories contributed to", fmt(repos)),
        ("prs", "Pull requests merged", fmt(prs)),
        ("issues", "Issues opened", fmt(issues)),
    ]
    rows_html = "".join(
        f"""
<tr><td class="label">{OCTICONS[key]}{label}</td><td class="value">{value}</td></tr>"""
        for key, label, value in items
    )

    return card(
        f"""<style>
table {{
  width: 100%;
  border-collapse: collapse;
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
  white-space: nowrap;
}}
.additions {{ color: #3fb950; }}
.deletions {{ color: #f85149; }}
.octicon {{ fill: rgb(139, 139, 139); }}
</style>
<div xmlns="http://www.w3.org/1999/xhtml">
<h2>GitHub Activity</h2>
<table>
<tbody>{rows_html}
</tbody>
</table>
</div>"""
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
</div>"""
    )


def main() -> None:
    print("Fetching all-time authored commit, PR, and issue counts...")
    stats = get_overview()
    print(f"  Overview: {stats}")

    print("Fetching repos...")
    repos = get_all_repos()
    print(f"  Public non-fork repos: {len(repos)}")

    print("Fetching languages...")
    languages: defaultdict[str, int] = defaultdict(int)
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        for lang_data in executor.map(get_repo_languages, repos):
            for lang, bytes_ in lang_data.items():
                languages[lang] += bytes_

    print("Generating SVGs...")
    svg = render_overview(stats)
    svg_langs = render_languages(dict(languages))
    overview_path = OUTPUT / "generated.overview.svg"
    _ = overview_path.write_text(svg, encoding="utf-8")
    print(f"  Written to {overview_path}")

    langs_path = OUTPUT / "generated.languages.svg"
    _ = langs_path.write_text(svg_langs, encoding="utf-8")
    print(f"  Written to {langs_path}")

    print("Done!")


if __name__ == "__main__":
    main()
