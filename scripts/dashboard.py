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

# A codex run is treated as "in progress" if its .jsonl was touched within
# this window AND no sibling .meta.json exists yet. .meta.json is written
# only after codex returns and the comment/calendar are persisted, so its
# absence + a fresh jsonl is a strong signal that codex is still streaming.
RUNNING_FRESH_SECONDS = 300

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


def collect_running(now: datetime) -> list[dict]:
    if not LOG_DIR.exists():
        return []
    cutoff = now.timestamp() - RUNNING_FRESH_SECONDS
    running: list[dict] = []
    for path in LOG_DIR.iterdir():
        name = path.name
        if not name.endswith(".jsonl"):
            continue
        stem = name[: -len(".jsonl")]
        if (LOG_DIR / f"{stem}.meta.json").exists():
            continue
        try:
            st = path.stat()
        except OSError:
            continue
        if st.st_mtime < cutoff:
            continue
        fn_info = parse_filename(stem)
        if not fn_info:
            continue
        running.append({
            **fn_info,
            "jsonl_path": str(path),
            "jsonl_size": st.st_size,
            "last_active": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
        })
    running.sort(key=lambda r: r["last_active"], reverse=True)
    return running


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
    .running-section {
      margin: 1rem 0; padding: .75rem 1rem; border: 1px solid #ffd58a;
      background: #fff8e6; border-radius: 6px;
    }
    .running-section h2 {
      margin: 0 0 .5rem; font-size: .95rem; color: #8a5a00;
      display: flex; align-items: center; gap: .5rem;
    }
    .pulse {
      display: inline-block; width: .55rem; height: .55rem; border-radius: 50%;
      background: #e07a00; box-shadow: 0 0 0 0 rgba(224, 122, 0, 0.6);
      animation: pulse 1.4s infinite;
    }
    @keyframes pulse {
      0%   { box-shadow: 0 0 0 0   rgba(224, 122, 0, 0.55); }
      70%  { box-shadow: 0 0 0 8px rgba(224, 122, 0, 0);    }
      100% { box-shadow: 0 0 0 0   rgba(224, 122, 0, 0);    }
    }
    .running-item {
      padding: .35rem 0; border-bottom: 1px dashed #f0d9a8;
      font-size: .88rem; display: flex; gap: .6rem; flex-wrap: wrap;
      align-items: baseline;
    }
    .running-item:last-child { border-bottom: none; }
    .running-item .meta { color: #8a5a00; }
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

  <div id="running"></div>

  <div class="filter-bar">
    <label>时间范围：
      <select id="range">
        <option value="today" selected>今天</option>
        <option value="week">本周</option>
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

  <script id="reviews-data" type="application/json">__REVIEWS_JSON__</script>
  <script id="running-data" type="application/json">__RUNNING_JSON__</script>
  <script>
    // Data island pattern: read JSON from a <script type="application/json">
    // tag via textContent + JSON.parse. This keeps PR-controlled strings
    // (titles, comment bodies) out of the JS source position. Defense in
    // depth: render_html also escapes "<\/" before substitution, so an
    // attacker cannot terminate the data island with a literal end tag
    // even if the parser ignores JS-comment semantics (which it does — it
    // ends ANY <script> on the next end tag regardless of context).
    const reviews = JSON.parse(document.getElementById('reviews-data').textContent);
    const running = JSON.parse(document.getElementById('running-data').textContent);
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

    function rangeCutoff(range) {
      if (range === 'today') {
        const d = new Date(); d.setHours(0, 0, 0, 0);
        return d;
      }
      if (range === 'week') {
        // 本周一 00:00（中国习惯：周一为一周第一天）
        const d = new Date();
        const day = d.getDay() || 7;  // 周日(0) 视作 7
        d.setDate(d.getDate() - day + 1);
        d.setHours(0, 0, 0, 0);
        return d;
      }
      return null;  // all
    }

    function getFiltered() {
      const range = document.getElementById('range').value;
      const repo = document.getElementById('repo-filter').value;
      const q = document.getElementById('q').value.trim().toLowerCase();
      let out = reviews;
      const cutoff = rangeCutoff(range);
      if (cutoff) {
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

    function escapeAttr(s) {
      return String(s == null ? '' : s)
        .replaceAll('&', '&amp;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#39;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;');
    }

    function safeUrl(u) {
      if (u == null) return '';
      try {
        const url = new URL(u);
        if (url.protocol !== 'https:' && url.protocol !== 'http:') return '';
        return url.toString();
      } catch (e) {
        return '';
      }
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
      const sCommentUrl = safeUrl(r.comment_url);
      if (sCommentUrl) {
        links.push('<a href="' + escapeAttr(sCommentUrl) + '" target="_blank" rel="noopener noreferrer">评论</a>');
      }
      const sPrUrl = safeUrl(r.pr_url);
      if (sPrUrl) {
        links.push('<a href="' + escapeAttr(sPrUrl) + '" target="_blank" rel="noopener noreferrer">PR</a>');
      }
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
      const safe = safeUrl(url);
      const open = safe ? '<a href="' + escapeAttr(safe) + '" target="_blank" rel="noopener noreferrer">' : '<span>';
      const close = safe ? '</a>' : '</span>';
      return (
        open + escapeHtml(repo) + ' #' + escapeHtml(number) + close +
        titleHtml +
        (extra ? ' <span class="group-count">' + escapeHtml(extra) + '</span>' : '')
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

    function fmtElapsed(iso) {
      const t = new Date(iso).getTime();
      if (!t) return '';
      const sec = Math.max(0, Math.floor((Date.now() - t) / 1000));
      if (sec < 60) return sec + 's 前';
      const m = Math.floor(sec / 60);
      if (m < 60) return m + 'min 前';
      const h = Math.floor(m / 60);
      return h + 'h' + (m % 60) + 'min 前';
    }

    function fmtBytes(n) {
      if (n < 1024) return n + 'B';
      if (n < 1024 * 1024) return (n / 1024).toFixed(1) + 'KB';
      return (n / 1024 / 1024).toFixed(1) + 'MB';
    }

    function renderRunning() {
      const root = document.getElementById('running');
      if (!running || running.length === 0) {
        root.innerHTML = '';
        return;
      }
      const items = running.map(r => {
        const sPrUrl = safeUrl(r.pr_url);
        const link = sPrUrl
          ? '<a href="' + escapeAttr(sPrUrl) + '" target="_blank" rel="noopener noreferrer">' +
            escapeHtml(r.repo) + ' #' + escapeHtml(r.pr_number) + '</a>'
          : escapeHtml(r.repo) + ' #' + escapeHtml(r.pr_number);
        return (
          '<div class="running-item">' +
            link +
            ' <span class="meta">started ' + escapeHtml((r.timestamp || '').replace('T', ' ').slice(11, 19)) +
            ' · 最近活动 ' + escapeHtml(fmtElapsed(r.last_active)) +
            ' · jsonl ' + escapeHtml(fmtBytes(r.jsonl_size || 0)) + '</span>' +
          '</div>'
        );
      }).join('');
      root.innerHTML = (
        '<div class="running-section">' +
          '<h2><span class="pulse"></span>运行中 (' + running.length + ')</h2>' +
          items +
        '</div>'
      );
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

    renderRunning();
    render();
  </script>
</body>
</html>
"""


def _safe_json(obj) -> str:
    # The data island is still inside a <script> tag, and the HTML parser
    # ends ANY <script> on the next literal "</script>" — even inside what
    # looks like a JSON string. Neutralize "</" → "<\/" before substitution
    # (legal in JSON, harmless to JSON.parse, but no longer matches the
    # HTML end-tag scanner). This is the defense paired with the
    # JSON.parse(textContent) island in HTML_TEMPLATE.
    return json.dumps(obj, ensure_ascii=False, default=str).replace("</", "<\\/")


def render_html(reviews: list[dict], running: list[dict]) -> str:
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return (HTML_TEMPLATE
            .replace("__REVIEWS_JSON__", _safe_json(reviews))
            .replace("__RUNNING_JSON__", _safe_json(running))
            .replace("__GENERATED_AT__", generated_at))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--open", action="store_true", help="open the dashboard in default browser after writing")
    parser.add_argument("--dry-run", action="store_true", help="collect data and print stats, do not write the HTML file")
    args = parser.parse_args()

    reviews = collect_reviews()
    running = collect_running(datetime.now())

    if args.dry_run:
        print(f"[dashboard] dry-run: would render {len(reviews)} review(s), {len(running)} running")
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
        for r in running:
            print(f"  running: {r['repo']} #{r['pr_number']}  last_active={r['last_active']}  jsonl={r['jsonl_size']}B")
        print(f"  output (skipped): {OUTPUT_PATH}")
        return 0

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    html = render_html(reviews, running)
    OUTPUT_PATH.write_text(html, encoding="utf-8")
    print(f"[dashboard] wrote {len(reviews)} review(s), {len(running)} running → {OUTPUT_PATH}")

    if args.open:
        subprocess.run(["open", str(OUTPUT_PATH)], check=False)

    return 0


if __name__ == "__main__":
    sys.exit(main())
