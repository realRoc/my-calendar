"""Generate a local HTML dashboard of all PR reviews.

Reads from pr_logs/ (no new storage):
- *.meta.json  → canonical per-run record (sidecar written by pr_watcher.process_pr)
- *.last.txt   → fallback comment body for runs without a sidecar (older history)
- filename     → fallback timestamp / repo / PR number when no sidecar exists

Writes a single self-contained HTML file to pr_logs/pr-dashboard.html.
No server, no background process — re-run to refresh.

Usage:
  python scripts/dashboard.py              # generate the dashboard, print its path
  python scripts/dashboard.py --open       # generate and open in default browser
  python scripts/dashboard.py --dry-run    # collect data, print stats, do not write file
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOG_DIR = HERE / "pr_logs"
OUTPUT_PATH = LOG_DIR / "pr-dashboard.html"

# Matches  20260519-234207__floatmiracle_askmanyai_pull_41
FILENAME_RE = re.compile(r"^(\d{8}-\d{6})__(.+)_pull_(\d+)$")


def parse_filename(stem: str) -> dict | None:
    m = FILENAME_RE.match(stem)
    if not m:
        return None
    ts_raw, owner_repo, number = m.groups()
    try:
        ts = datetime.strptime(ts_raw, "%Y%m%d-%H%M%S")
    except ValueError:
        return None
    parts = owner_repo.split("_", 1)
    if len(parts) != 2:
        return None
    owner, repo_name = parts
    return {
        "timestamp": ts.isoformat(timespec="seconds"),
        "repo": f"{owner}/{repo_name}",
        "pr_number": int(number),
        "pr_url": f"https://github.com/{owner}/{repo_name}/pull/{number}",
    }


def collect_reviews() -> list[dict]:
    if not LOG_DIR.exists():
        return []

    # Group sibling files by stem (anything before the recognized extension)
    runs: dict[str, dict[str, Path]] = {}
    for path in LOG_DIR.iterdir():
        name = path.name
        for ext in (".meta.json", ".last.txt", ".jsonl"):
            if name.endswith(ext):
                stem = name[: -len(ext)]
                runs.setdefault(stem, {})[ext] = path
                break

    reviews: list[dict] = []
    for stem, parts in runs.items():
        # A "review run" is anchored by either a sidecar or a .last.txt.
        # Bare .jsonl (codex started but never produced output) is skipped.
        if ".meta.json" not in parts and ".last.txt" not in parts:
            continue

        rec: dict = {}
        if ".meta.json" in parts:
            try:
                rec = json.loads(parts[".meta.json"].read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                rec = {}

        # Map sidecar "started_at" → "timestamp" for uniform key
        if "timestamp" not in rec and "started_at" in rec:
            rec["timestamp"] = rec["started_at"]

        # Fill missing structural fields from the filename (older history)
        fn_info = parse_filename(stem) or {}
        for key in ("timestamp", "repo", "pr_number", "pr_url"):
            if not rec.get(key) and key in fn_info:
                rec[key] = fn_info[key]

        # Fallback comment body from .last.txt for sidecar-less runs
        if not rec.get("comment_body") and ".last.txt" in parts:
            try:
                rec["comment_body"] = parts[".last.txt"].read_text(encoding="utf-8").strip()
            except OSError:
                rec["comment_body"] = ""

        if not rec.get("jsonl_path") and ".jsonl" in parts:
            rec["jsonl_path"] = str(parts[".jsonl"])

        if "timestamp" not in rec or "repo" not in rec:
            continue

        reviews.append(rec)

    reviews.sort(key=lambda r: r.get("timestamp", ""), reverse=True)
    return reviews


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>PR Review 看板</title>
  <style>
    :root { color-scheme: light; }
    * { box-sizing: border-box; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Helvetica Neue", "PingFang SC", sans-serif;
      max-width: 1200px; margin: 2rem auto; padding: 0 1.25rem;
      color: #1a1a1a; line-height: 1.5;
    }
    h1 { margin: 0 0 .25rem; font-size: 1.5rem; }
    .meta { color: #888; font-size: .85rem; }
    .filter-bar {
      display: flex; flex-wrap: wrap; gap: 1rem; align-items: center;
      margin: 1.25rem 0 .75rem; padding: .75rem; background: #f7f7f8; border-radius: 6px;
    }
    .filter-bar label { font-size: .9rem; color: #555; }
    .filter-bar select, .filter-bar input {
      font: inherit; padding: .25rem .4rem; border: 1px solid #ccc; border-radius: 4px;
      background: white;
    }
    .tabs { display: flex; gap: .25rem; border-bottom: 1px solid #ddd; margin-bottom: .75rem; }
    .tab {
      padding: .5rem .9rem; cursor: pointer; font-size: .9rem; color: #555;
      border-bottom: 2px solid transparent;
    }
    .tab.active { color: #0066cc; border-bottom-color: #0066cc; font-weight: 600; }
    .tab:hover { background: #f7f7f8; }
    .group { margin-bottom: 1.25rem; }
    .group-header {
      font-weight: 600; padding: .5rem 0; border-bottom: 1px solid #eee;
      display: flex; align-items: baseline; gap: .5rem; flex-wrap: wrap;
    }
    .group-count { color: #888; font-size: .85rem; font-weight: normal; }
    .pr-title { color: #555; font-weight: normal; font-size: .9rem; }
    .review {
      padding: .5rem .75rem; border-bottom: 1px solid #f0f0f0;
      cursor: pointer; transition: background .1s;
    }
    .review:hover { background: #fafafa; }
    .review.open { background: #f5f9ff; }
    .review-meta {
      font-size: .85rem; color: #555;
      display: flex; gap: .5rem; flex-wrap: wrap; align-items: center;
    }
    .body-wrap { max-height: 0; overflow: hidden; transition: max-height .2s ease; }
    .review.open .body-wrap { max-height: 70vh; overflow-y: auto; }
    .body-wrap pre {
      margin: .5rem 0 .25rem;
      background: #fff; border: 1px solid #e7e7e7; padding: .75rem;
      border-radius: 4px; white-space: pre-wrap; word-break: break-word;
      font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
      font-size: .82rem; line-height: 1.45;
    }
    a { color: #0066cc; text-decoration: none; }
    a:hover { text-decoration: underline; }
    .badge {
      display: inline-block; padding: .05rem .4rem; border-radius: 3px;
      font-size: .72rem; font-weight: 500;
    }
    .badge-ok  { background: #e2f6e8; color: #1d6f3d; }
    .badge-err { background: #fde6e6; color: #a4262c; }
    .badge-skip{ background: #eee; color: #666; }
    code {
      background: #f0f0f0; padding: .05rem .3rem; border-radius: 3px;
      font-family: ui-monospace, monospace; font-size: .78rem;
    }
    .empty { text-align: center; color: #888; padding: 3rem 0; }
  </style>
</head>
<body>
  <h1>PR Review 看板</h1>
  <div class="meta">生成于 <span id="generated-at"></span> · 共 <span id="total"></span> 条 review</div>

  <div class="filter-bar">
    <label>时间范围：
      <select id="range">
        <option value="7">近 7 天</option>
        <option value="30" selected>近 30 天</option>
        <option value="90">近 90 天</option>
        <option value="all">全部</option>
      </select>
    </label>
    <label>repo：
      <select id="repo-filter"><option value="">全部</option></select>
    </label>
    <label>搜索：
      <input id="q" placeholder="标题 / 评论内容" size="24">
    </label>
  </div>

  <div class="tabs">
    <div class="tab active" data-view="repo">按 Repo</div>
    <div class="tab" data-view="pr">按 PR</div>
    <div class="tab" data-view="timeline">时间线</div>
  </div>

  <div id="content"></div>

  <script>
    const reviews = __REVIEWS_JSON__;
    const generatedAt = "__GENERATED_AT__";

    document.getElementById('generated-at').textContent = generatedAt;
    document.getElementById('total').textContent = reviews.length;

    const repos = [...new Set(reviews.map(r => r.repo))].sort();
    const repoSelect = document.getElementById('repo-filter');
    repos.forEach(r => {
      const opt = document.createElement('option');
      opt.value = r; opt.textContent = r;
      repoSelect.appendChild(opt);
    });

    let currentView = 'repo';

    function getFiltered() {
      const range = document.getElementById('range').value;
      const repo = document.getElementById('repo-filter').value;
      const q = document.getElementById('q').value.trim().toLowerCase();
      let out = reviews;
      if (range !== 'all') {
        const cutoff = new Date(Date.now() - parseInt(range, 10) * 86400000);
        out = out.filter(r => new Date(r.timestamp) >= cutoff);
      }
      if (repo) out = out.filter(r => r.repo === repo);
      if (q) {
        out = out.filter(r =>
          (r.pr_title || '').toLowerCase().includes(q) ||
          (r.comment_body || '').toLowerCase().includes(q)
        );
      }
      return out;
    }

    function escapeHtml(s) {
      const div = document.createElement('div');
      div.textContent = s == null ? '' : String(s);
      return div.innerHTML;
    }

    function exitBadge(exit) {
      if (exit === 0) return '<span class="badge badge-ok">OK</span>';
      if (exit == null) return '<span class="badge badge-skip">no-meta</span>';
      return '<span class="badge badge-err">exit=' + exit + '</span>';
    }

    function reviewRow(r) {
      const sha = (r.head_sha || '').slice(0, 8);
      const ts = (r.timestamp || '').replace('T', ' ').slice(0, 19);
      const links = [];
      if (r.comment_url) links.push('<a href="' + r.comment_url + '" target="_blank">评论</a>');
      links.push('<a href="' + r.pr_url + '" target="_blank">PR</a>');
      if (r.thread_id) links.push('<code>codex resume ' + escapeHtml(r.thread_id) + '</code>');
      return (
        '<div class="review" onclick="this.classList.toggle(\'open\')">' +
          '<div class="review-meta">' +
            ts + (sha ? ' · ' + sha : '') + ' ' + exitBadge(r.codex_exit) + ' · ' + links.join(' · ') +
          '</div>' +
          '<div class="body-wrap"><pre>' + escapeHtml(r.comment_body || '(无 comment body)') + '</pre></div>' +
        '</div>'
      );
    }

    function prHeader(repo, number, title, url, extra) {
      const titleHtml = title ? ' · <span class="pr-title">' + escapeHtml(title) + '</span>' : '';
      return (
        '<a href="' + url + '" target="_blank">' + escapeHtml(repo) + ' #' + number + '</a>' +
        titleHtml +
        (extra ? ' <span class="group-count">' + extra + '</span>' : '')
      );
    }

    function render() {
      const data = getFiltered();
      const root = document.getElementById('content');
      if (data.length === 0) {
        root.innerHTML = '<div class="empty">这个范围内没有 review 记录</div>';
        return;
      }

      if (currentView === 'repo') {
        const byRepo = {};
        data.forEach(r => { (byRepo[r.repo] = byRepo[r.repo] || []).push(r); });
        root.innerHTML = Object.keys(byRepo).sort().map(repo => {
          const items = byRepo[repo];
          return (
            '<div class="group">' +
              '<div class="group-header">' + escapeHtml(repo) +
                ' <span class="group-count">(' + items.length + ')</span></div>' +
              items.map(reviewRow).join('') +
            '</div>'
          );
        }).join('');
      } else if (currentView === 'pr') {
        const byPR = {};
        data.forEach(r => { (byPR[r.pr_url] = byPR[r.pr_url] || []).push(r); });
        const keys = Object.keys(byPR).sort((a, b) =>
          (byPR[b][0].timestamp || '').localeCompare(byPR[a][0].timestamp || '')
        );
        root.innerHTML = keys.map(url => {
          const items = byPR[url];
          const head = items[0];
          return (
            '<div class="group">' +
              '<div class="group-header">' +
                prHeader(head.repo, head.pr_number, head.pr_title, url,
                  '(' + items.length + ' review' + (items.length > 1 ? 's' : '') + ')') +
              '</div>' +
              items.map(reviewRow).join('') +
            '</div>'
          );
        }).join('');
      } else {
        root.innerHTML = data.map(r => (
          '<div class="group">' +
            '<div class="group-header">' +
              prHeader(r.repo, r.pr_number, r.pr_title, r.pr_url, '') +
            '</div>' +
            reviewRow(r) +
          '</div>'
        )).join('');
      }
    }

    document.querySelectorAll('.tab').forEach(t => {
      t.addEventListener('click', () => {
        document.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
        t.classList.add('active');
        currentView = t.dataset.view;
        render();
      });
    });
    document.getElementById('range').addEventListener('change', render);
    document.getElementById('repo-filter').addEventListener('change', render);
    document.getElementById('q').addEventListener('input', render);

    render();
  </script>
</body>
</html>
"""


def render_html(reviews: list[dict]) -> str:
    payload = json.dumps(reviews, ensure_ascii=False, default=str)
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return (HTML_TEMPLATE
            .replace("__REVIEWS_JSON__", payload)
            .replace("__GENERATED_AT__", generated_at))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--open", action="store_true", help="open the dashboard in default browser after writing")
    parser.add_argument("--dry-run", action="store_true", help="collect data and print stats, do not write the HTML file")
    args = parser.parse_args()

    reviews = collect_reviews()

    if args.dry_run:
        print(f"[dashboard] dry-run: would render {len(reviews)} review(s)")
        by_repo: dict[str, int] = {}
        with_sidecar = 0
        for r in reviews:
            by_repo[r["repo"]] = by_repo.get(r["repo"], 0) + 1
            if r.get("codex_exit") is not None:
                with_sidecar += 1
        for repo, n in sorted(by_repo.items()):
            print(f"  {repo}: {n}")
        if reviews:
            print(f"  with sidecar: {with_sidecar}/{len(reviews)}")
            print(f"  newest: {reviews[0]['timestamp']}  ({reviews[0]['repo']} #{reviews[0].get('pr_number')})")
            print(f"  oldest: {reviews[-1]['timestamp']}  ({reviews[-1]['repo']} #{reviews[-1].get('pr_number')})")
        print(f"  output (skipped): {OUTPUT_PATH}")
        return 0

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    html = render_html(reviews)
    OUTPUT_PATH.write_text(html, encoding="utf-8")
    print(f"[dashboard] wrote {len(reviews)} review(s) → {OUTPUT_PATH}")

    if args.open:
        subprocess.run(["open", str(OUTPUT_PATH)], check=False)

    return 0


if __name__ == "__main__":
    sys.exit(main())
