#!/usr/bin/env python3
import argparse
import email.utils
import hashlib
import html as html_lib
import json
import re
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urljoin, urlparse

import requests
import yaml


UTC = timezone.utc


def now_utc() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).replace(microsecond=0).isoformat()


def parse_dt(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    value = value.strip()
    for fmt in ("%Y%m%dT%H%M%SZ", "%Y%m%d%H%M%S"):
        try:
            return iso(datetime.strptime(value, fmt).replace(tzinfo=UTC))
        except ValueError:
            pass
    try:
        parsed = email.utils.parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return iso(parsed)
    except Exception:
        return None


def stable_hash(*parts: str) -> str:
    h = hashlib.sha256()
    for part in parts:
        h.update((part or "").strip().encode("utf-8", errors="ignore"))
        h.update(b"\0")
    return h.hexdigest()


def load_config(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def init_db(db_path: str) -> None:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(db_path)) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS articles (
              id TEXT PRIMARY KEY,
              source TEXT NOT NULL,
              topic TEXT NOT NULL,
              query TEXT,
              title TEXT,
              url TEXT,
              domain TEXT,
              language TEXT,
              published_at TEXT,
              collected_at TEXT NOT NULL,
              snippet TEXT,
              raw_json TEXT
            )
            """
        )
        db.execute("CREATE INDEX IF NOT EXISTS idx_articles_topic_time ON articles(topic, collected_at)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_articles_url ON articles(url)")
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS state_snapshots (
              id TEXT PRIMARY KEY,
              created_at TEXT NOT NULL,
              state_json TEXT NOT NULL
            )
            """
        )
        db.commit()


def request_session(config: Dict[str, Any]) -> requests.Session:
    s = requests.Session()
    ua = config.get("collector", {}).get("user_agent", "world-state-vps/0.1")
    s.headers.update({"User-Agent": ua})
    return s


def fetch_gdelt(session: requests.Session, config: Dict[str, Any], topic: Dict[str, Any]) -> List[Dict[str, Any]]:
    collector = config.get("collector", {})
    timeout = int(collector.get("request_timeout_seconds", 20))
    params = {
        "query": topic["gdelt_query"],
        "mode": "artlist",
        "format": "json",
        "timespan": collector.get("gdelt_timespan", "1h"),
        "sort": "datedesc",
        "maxrecords": int(collector.get("max_articles_per_query", 50)),
    }
    r = session.get("https://api.gdeltproject.org/api/v2/doc/doc", params=params, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    out = []
    for item in data.get("articles", []):
        url = item.get("url") or ""
        title = item.get("title") or ""
        out.append(
            {
                "source": "gdelt_doc",
                "topic": topic["name"],
                "query": topic["gdelt_query"],
                "title": title,
                "url": url,
                "domain": item.get("domain") or urlparse(url).netloc,
                "language": item.get("language"),
                "published_at": parse_dt(item.get("seendate")),
                "snippet": item.get("sourcecountry") or "",
                "raw": item,
            }
        )
    return out


def fetch_rss(session: requests.Session, config: Dict[str, Any], feed: Dict[str, Any]) -> List[Dict[str, Any]]:
    timeout = int(config.get("collector", {}).get("request_timeout_seconds", 20))
    r = session.get(feed["url"], timeout=timeout)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    items = root.findall(".//item")
    if not items:
        items = root.findall("{http://www.w3.org/2005/Atom}entry")
    out = []
    max_items = int(feed.get("max_items", 100))
    detail_delay = float(feed.get("detail_delay_seconds", 0.5))
    for item in items[:max_items]:
        title = text_of(item, "title")
        link = text_of(item, "link")
        if not link:
            atom_link = item.find("{http://www.w3.org/2005/Atom}link")
            link = atom_link.attrib.get("href", "") if atom_link is not None else ""
        published = text_of(item, "pubDate") or text_of(item, "published") or text_of(item, "updated")
        desc = text_of(item, "description") or text_of(item, "summary")
        detail = {"full_text": "", "description": "", "body_html": ""}
        if feed.get("fetch_full_text") and link:
            try:
                detail = fetch_article_detail(session, config, link, feed.get("parser", feed.get("name", "generic")))
            except Exception as exc:
                print(f"rss detail fetch failed feed={feed.get('name')} url={link}: {exc}", file=sys.stderr)
            time.sleep(detail_delay)
        snippet_source = detail.get("description") or detail.get("full_text") or desc or title
        out.append(
            {
                "source": f"rss:{feed['name']}",
                "topic": feed.get("topic", feed["name"]),
                "query": feed["url"],
                "title": title,
                "url": link,
                "domain": urlparse(link).netloc,
                "language": None,
                "published_at": parse_dt(published),
                "snippet": clean_text(snippet_source)[:500],
                "raw": {
                    "feed": feed["name"],
                    "description": desc,
                    "full_text": detail.get("full_text", ""),
                    "body_html": detail.get("body_html", ""),
                },
            }
        )
    return out


def text_of(node: ET.Element, name: str) -> str:
    found = node.find(name)
    if found is None:
        found = node.find("{http://www.w3.org/2005/Atom}" + name)
    return clean_text(found.text if found is not None and found.text else "")


def html_to_text(fragment: str) -> str:
    fragment = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", fragment)
    fragment = re.sub(r"(?i)<br\s*/?>", "\n", fragment)
    fragment = re.sub(r"(?i)</(p|div|li|h1|h2|h3|h4|blockquote|tr)>", "\n", fragment)
    fragment = re.sub(r"(?is)<[^>]+>", " ", fragment)
    return clean_text(html_lib.unescape(fragment))


def extract_div_by_class(html: str, class_substring: str) -> str:
    m = re.search(r'<div\b[^>]*class="[^"]*' + re.escape(class_substring) + r'[^"]*"[^>]*>', html, re.I)
    if not m:
        return ""
    pos = m.end()
    depth = 1
    for tag in re.finditer(r"(?is)</?div\b[^>]*>", html[pos:]):
        full_tag = tag.group(0)
        if full_tag.startswith("</"):
            depth -= 1
            if depth == 0:
                return html[m.start():pos + tag.end()]
        else:
            depth += 1
    return html[m.start():]


def extract_div_by_id(html: str, id_value: str) -> str:
    m = re.search(r'<div\b[^>]*id=["\']' + re.escape(id_value) + r'["\'][^>]*>', html, re.I)
    if not m:
        return ""
    pos = m.end()
    depth = 1
    for tag in re.finditer(r"(?is)</?div\b[^>]*>", html[pos:]):
        full_tag = tag.group(0)
        if full_tag.startswith("</"):
            depth -= 1
            if depth == 0:
                return html[m.start():pos + tag.end()]
        else:
            depth += 1
    return html[m.start():]


def meta_content(html: str, name: str) -> str:
    patterns = [
        r'<meta\s+property=["\']' + re.escape(name) + r'["\']\s+content=["\']([^"\']*)["\']',
        r'<meta\s+name=["\']' + re.escape(name) + r'["\']\s+content=["\']([^"\']*)["\']',
        r'<meta\s+content=["\']([^"\']*)["\']\s+(?:property|name)=["\']' + re.escape(name) + r'["\']',
    ]
    for pattern in patterns:
        m = re.search(pattern, html, re.I)
        if m:
            return clean_text(html_lib.unescape(m.group(1)))
    return ""


def extract_article_body(html: str, parser: str = "generic") -> Dict[str, str]:
    candidates = []
    if parser in {"sec_press", "sec"}:
        candidates.append(extract_div_by_class(html, "field--name-body"))
    elif parser in {"fed_press", "fed"}:
        article = extract_div_by_id(html, "article")
        body = ""
        matches = re.findall(r'(?is)<div\b[^>]*class="[^"]*col-xs-12\s+col-sm-8\s+col-md-8[^"]*"[^>]*>(.*?)</div>', article)
        if matches:
            body = matches[-1]
        candidates.extend([body, article])
    elif parser in {"whitehouse_briefings", "whitehouse"}:
        candidates.append(extract_div_by_class(html, "entry-content"))
    elif parser in {"treasury_press_releases", "treasury"}:
        candidates.append(extract_div_by_class(html, "field--name-field-news-body"))
    elif parser in {"ofac_recent_actions", "ofac"}:
        candidates.append(extract_div_by_class(html, "field--name-body"))
    candidates.extend([
        extract_div_by_class(html, "field--name-body"),
        extract_div_by_class(html, "entry-content"),
        extract_div_by_id(html, "article"),
        extract_div_by_id(html, "main-content"),
    ])
    body_html = next((c for c in candidates if html_to_text(c)), "")
    full_text = html_to_text(body_html)
    description = meta_content(html, "og:description") or meta_content(html, "description")
    return {"full_text": full_text, "description": description, "body_html": body_html[:200000]}


def fetch_article_detail(session: requests.Session, config: Dict[str, Any], url: str, parser: str = "generic") -> Dict[str, str]:
    timeout = int(config.get("collector", {}).get("request_timeout_seconds", 20))
    r = session.get(url, timeout=timeout)
    r.raise_for_status()
    return extract_article_body(r.text, parser)


def classify_treasury_title(title: str) -> str:
    tl = title.lower()
    if "sanction" in tl or "ofac" in tl:
        return "sanctions"
    if any(x in tl for x in ["borrowing", "refunding", "treasury securities", "tbac"]):
        return "debt_issuance"
    if "fincen" in tl:
        return "fincen"
    if "cfius" in tl:
        return "cfius"
    if any(x in tl for x in ["iran", "russia", "china", "congo", "ukraine"]):
        return "geo_finance"
    if "appointment" in tl or "nomination" in tl:
        return "appointments"
    if "remarks" in tl or "statement" in tl:
        return "remarks"
    return "treasury_policy"


def fetch_treasury_article_body(session: requests.Session, config: Dict[str, Any], url: str) -> Dict[str, str]:
    return fetch_article_detail(session, config, url, "treasury_press_releases")


def fetch_treasury_press_releases(session: requests.Session, config: Dict[str, Any], feed: Dict[str, Any]) -> List[Dict[str, Any]]:
    timeout = int(config.get("collector", {}).get("request_timeout_seconds", 20))
    r = session.get(feed["url"], timeout=timeout)
    r.raise_for_status()
    page_html = r.text
    start = page_html.find('<div class="content--2col__body">')
    if start == -1:
        start = page_html.find('id="main-content"')
    end = page_html.find('<nav class="pager"', start)
    if end == -1:
        end = page_html.find('</section>', start)
    main = page_html[start:end if end != -1 else len(page_html)]
    pattern = re.compile(
        r'<time[^>]*datetime="([^"]+)"[^>]*>.*?</time>.*?'
        r'<h3[^>]*class="[^"]*featured-stories__headline[^"]*"[^>]*>\s*'
        r'<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
        re.S | re.I,
    )
    out: List[Dict[str, Any]] = []
    seen = set()
    max_items = int(feed.get("max_items", 10))
    detail_delay = float(feed.get("detail_delay_seconds", 1.0))
    for published, href, title_html in pattern.findall(main):
        title = html_to_text(title_html)
        link = urljoin(feed["url"], html_lib.unescape(href))
        if link in seen:
            continue
        seen.add(link)
        category = classify_treasury_title(title)
        detail = {"full_text": "", "description": "", "body_html": ""}
        try:
            detail = fetch_treasury_article_body(session, config, link)
        except Exception as exc:
            print(f"treasury detail fetch failed url={link}: {exc}", file=sys.stderr)
        snippet_source = detail.get("description") or detail.get("full_text") or title
        out.append(
            {
                "source": f"html:{feed['name']}",
                "topic": feed.get("topic", feed["name"]),
                "query": feed["url"],
                "title": title,
                "url": link,
                "domain": urlparse(link).netloc,
                "language": "en",
                "published_at": parse_dt(published),
                "snippet": clean_text(snippet_source)[:500],
                "raw": {
                    "feed": feed["name"],
                    "category": category,
                    "published_at_raw": published,
                    "description": detail.get("description", ""),
                    "full_text": detail.get("full_text", ""),
                    "body_html": detail.get("body_html", ""),
                },
            }
        )
        if len(out) >= max_items:
            break
        time.sleep(detail_delay)
    return out


def parse_ofac_recent_actions(page_html: str, base_url: str, max_items: int = 10) -> List[Dict[str, str]]:
    pattern = re.compile(
        r'<div[^>]*class="[^"]*views-row[^"]*"[^>]*>.*?'
        r'<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>.*?'
        r'([A-Z][a-z]+\s+\d{1,2},\s+\d{4})\s*-\s*<a[^>]*>(.*?)</a>',
        re.S | re.I,
    )
    rows = []
    seen = set()
    for href, title_html, date_text, category_html in pattern.findall(page_html):
        link = urljoin(base_url, html_lib.unescape(href))
        if link in seen:
            continue
        seen.add(link)
        rows.append(
            {
                "title": html_to_text(title_html),
                "url": link,
                "published_at": parse_dt(date_text) or "",
                "category": html_to_text(category_html),
            }
        )
        if len(rows) >= max_items:
            break
    return rows


def parse_whitehouse_briefings(page_html: str, base_url: str, max_items: int = 10) -> List[Dict[str, str]]:
    pattern = re.compile(r'<a[^>]+href="([^"]*/briefings-statements/\d{4}/\d{2}/[^"]+/)"[^>]*>(.*?)</a>', re.S | re.I)
    rows = []
    seen = set()
    for href, title_html in pattern.findall(page_html):
        title = html_to_text(title_html)
        link = urljoin(base_url, html_lib.unescape(href))
        if not title or link in seen:
            continue
        seen.add(link)
        after = page_html[page_html.find(href):page_html.find(href) + 2500]
        m = re.search(r'<time[^>]*datetime="([^"]+)"', after, re.I)
        rows.append({"title": title, "url": link, "published_at": parse_dt(m.group(1)) if m else "", "category": "statements"})
        if len(rows) >= max_items:
            break
    return rows


def fetch_low_volume_html_articles(session: requests.Session, config: Dict[str, Any], feed: Dict[str, Any], rows: List[Dict[str, str]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    detail_delay = float(feed.get("detail_delay_seconds", 1.0))
    parser = feed.get("parser", "generic")
    for row in rows:
        detail = {"full_text": "", "description": "", "body_html": ""}
        try:
            detail = fetch_article_detail(session, config, row["url"], parser)
        except Exception as exc:
            print(f"html detail fetch failed feed={feed.get('name')} url={row.get('url')}: {exc}", file=sys.stderr)
        snippet_source = detail.get("description") or detail.get("full_text") or row.get("title", "")
        out.append(
            {
                "source": f"html:{feed['name']}",
                "topic": feed.get("topic", feed["name"]),
                "query": feed["url"],
                "title": row.get("title", ""),
                "url": row.get("url", ""),
                "domain": urlparse(row.get("url", "")).netloc,
                "language": "en",
                "published_at": row.get("published_at") or None,
                "snippet": clean_text(snippet_source)[:500],
                "raw": {
                    "feed": feed["name"],
                    "category": row.get("category", ""),
                    "description": detail.get("description", ""),
                    "full_text": detail.get("full_text", ""),
                    "body_html": detail.get("body_html", ""),
                },
            }
        )
        time.sleep(detail_delay)
    return out


def fetch_ofac_recent_actions(session: requests.Session, config: Dict[str, Any], feed: Dict[str, Any]) -> List[Dict[str, Any]]:
    timeout = int(config.get("collector", {}).get("request_timeout_seconds", 20))
    r = session.get(feed["url"], timeout=timeout)
    r.raise_for_status()
    rows = parse_ofac_recent_actions(r.text, feed["url"], int(feed.get("max_items", 10)))
    return fetch_low_volume_html_articles(session, config, feed, rows)


def fetch_whitehouse_briefings(session: requests.Session, config: Dict[str, Any], feed: Dict[str, Any]) -> List[Dict[str, Any]]:
    timeout = int(config.get("collector", {}).get("request_timeout_seconds", 20))
    r = session.get(feed["url"], timeout=timeout)
    r.raise_for_status()
    rows = parse_whitehouse_briefings(r.text, feed["url"], int(feed.get("max_items", 10)))
    return fetch_low_volume_html_articles(session, config, feed, rows)


def fetch_html_feed(session: requests.Session, config: Dict[str, Any], feed: Dict[str, Any]) -> List[Dict[str, Any]]:
    parser = feed.get("parser")
    if parser == "treasury_press_releases":
        return fetch_treasury_press_releases(session, config, feed)
    if parser == "ofac_recent_actions":
        return fetch_ofac_recent_actions(session, config, feed)
    if parser == "whitehouse_briefings":
        return fetch_whitehouse_briefings(session, config, feed)
    raise ValueError(f"unknown html feed parser: {parser}")


def clean_text(text: str) -> str:
    return " ".join((text or "").replace("\n", " ").split())


def store_articles(db_path: str, articles: Iterable[Dict[str, Any]]) -> int:
    collected_at = iso(now_utc())
    inserted = 0
    with closing(sqlite3.connect(db_path)) as db:
        for a in articles:
            article_id = stable_hash(a.get("url", ""), a.get("title", ""), a.get("source", ""))
            try:
                db.execute(
                    """
                    INSERT INTO articles
                    (id, source, topic, query, title, url, domain, language, published_at, collected_at, snippet, raw_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        article_id,
                        a.get("source"),
                        a.get("topic"),
                        a.get("query"),
                        a.get("title"),
                        a.get("url"),
                        a.get("domain"),
                        a.get("language"),
                        a.get("published_at"),
                        collected_at,
                        a.get("snippet"),
                        json.dumps(a.get("raw", {}), ensure_ascii=False),
                    ),
                )
                inserted += 1
            except sqlite3.IntegrityError:
                pass
        db.commit()
    return inserted


def row_dicts(cursor: sqlite3.Cursor) -> List[Dict[str, Any]]:
    cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, row)) for row in cursor.fetchall()]


def build_state(db_path: str, config: Dict[str, Any]) -> Dict[str, Any]:
    now = now_utc()
    since_24h = iso(now - timedelta(hours=24))
    since_1h = iso(now - timedelta(hours=1))
    topics = [t["name"] for t in config.get("topics", [])]
    rss_topics = [f.get("topic", f["name"]) for f in config.get("rss_feeds", [])]
    html_topics = [f.get("topic", f["name"]) for f in config.get("html_feeds", [])]
    all_topics = sorted(set(topics + rss_topics + html_topics))

    state = {
        "as_of": iso(now),
        "node": "vps_collector",
        "topics": {},
        "recent_headlines": [],
    }
    with closing(sqlite3.connect(db_path)) as db:
        for topic in all_topics:
            total_24h = db.execute(
                "SELECT COUNT(*) FROM articles WHERE topic=? AND collected_at>=?",
                (topic, since_24h),
            ).fetchone()[0]
            total_1h = db.execute(
                "SELECT COUNT(*) FROM articles WHERE topic=? AND collected_at>=?",
                (topic, since_1h),
            ).fetchone()[0]
            domains = row_dicts(
                db.execute(
                    """
                    SELECT domain, COUNT(*) AS count
                    FROM articles
                    WHERE topic=? AND collected_at>=? AND domain IS NOT NULL AND domain!=''
                    GROUP BY domain
                    ORDER BY count DESC
                    LIMIT 8
                    """,
                    (topic, since_24h),
                )
            )
            latest = row_dicts(
                db.execute(
                    """
                    SELECT title, url, domain, source, published_at, collected_at
                    FROM articles
                    WHERE topic=?
                    ORDER BY collected_at DESC
                    LIMIT 8
                    """,
                    (topic,),
                )
            )
            state["topics"][topic] = {
                "article_count_1h": total_1h,
                "article_count_24h": total_24h,
                "top_domains_24h": domains,
                "latest": latest,
            }
        state["recent_headlines"] = row_dicts(
            db.execute(
                """
                SELECT topic, title, url, domain, source, published_at, collected_at
                FROM articles
                WHERE collected_at>=?
                ORDER BY collected_at DESC
                LIMIT 40
                """,
                (since_1h,),
            )
        )

        snapshot_id = stable_hash(state["as_of"])
        db.execute(
            "INSERT OR REPLACE INTO state_snapshots (id, created_at, state_json) VALUES (?, ?, ?)",
            (snapshot_id, state["as_of"], json.dumps(state, ensure_ascii=False)),
        )
        db.commit()
    return state


def detect_alerts(db_path: str, config: Dict[str, Any]) -> List[Dict[str, Any]]:
    detector = config.get("detector", {})
    now = now_utc()
    recent_minutes = int(detector.get("recent_window_minutes", 60))
    baseline_hours = int(detector.get("baseline_window_hours", 72))
    min_recent = int(detector.get("min_recent_count", 8))
    surge_ratio = float(detector.get("surge_ratio", 3.0))
    keywords = [k.lower() for k in detector.get("high_priority_keywords", [])]
    recent_since = iso(now - timedelta(minutes=recent_minutes))
    baseline_since = iso(now - timedelta(hours=baseline_hours))
    alerts = []

    with closing(sqlite3.connect(db_path)) as db:
        topics = [r[0] for r in db.execute("SELECT DISTINCT topic FROM articles").fetchall()]
        for topic in topics:
            recent = db.execute(
                "SELECT COUNT(*) FROM articles WHERE topic=? AND collected_at>=?",
                (topic, recent_since),
            ).fetchone()[0]
            baseline = db.execute(
                "SELECT COUNT(*) FROM articles WHERE topic=? AND collected_at>=? AND collected_at<?",
                (topic, baseline_since, recent_since),
            ).fetchone()[0]
            baseline_rate = baseline / max((baseline_hours * 60 - recent_minutes) / recent_minutes, 1)
            ratio = recent / max(baseline_rate, 1)
            if recent >= min_recent and ratio >= surge_ratio:
                alerts.append(
                    {
                        "type": "coverage_surge",
                        "topic": topic,
                        "created_at": iso(now),
                        "severity": "high" if ratio >= surge_ratio * 2 else "medium",
                        "recent_count": recent,
                        "baseline_expected": round(baseline_rate, 2),
                        "ratio": round(ratio, 2),
                    }
                )

        if keywords:
            recent_rows = row_dicts(
                db.execute(
                    """
                    SELECT topic, title, url, domain, source, collected_at
                    FROM articles
                    WHERE collected_at>=?
                    ORDER BY collected_at DESC
                    LIMIT 300
                    """,
                    (recent_since,),
                )
            )
            for row in recent_rows:
                title_l = (row.get("title") or "").lower()
                hit = next((k for k in keywords if k in title_l), None)
                if hit:
                    alerts.append(
                        {
                            "type": "priority_keyword",
                            "topic": row.get("topic"),
                            "created_at": iso(now),
                            "severity": "medium",
                            "keyword": hit,
                            "title": row.get("title"),
                            "url": row.get("url"),
                            "domain": row.get("domain"),
                        }
                    )
    return alerts


def append_jsonl(path: Path, rows: Iterable[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def write_json(path: Path, data: Dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def cleanup(db_path: str, config: Dict[str, Any]) -> None:
    storage = config.get("storage", {})
    hot_days = int(storage.get("hot_retention_days", 14))
    snap_days = int(storage.get("snapshot_retention_days", 30))
    article_cutoff = iso(now_utc() - timedelta(days=hot_days))
    snapshot_cutoff = iso(now_utc() - timedelta(days=snap_days))
    with closing(sqlite3.connect(db_path)) as db:
        db.execute("DELETE FROM articles WHERE collected_at < ?", (article_cutoff,))
        db.execute("DELETE FROM state_snapshots WHERE created_at < ?", (snapshot_cutoff,))
        db.commit()
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument(
        "--sources",
        default="all",
        help="Comma-separated source groups to collect: gdelt,rss,html,all",
    )
    args = parser.parse_args()
    requested_sources = {x.strip().lower() for x in args.sources.split(",") if x.strip()}
    if not requested_sources or "all" in requested_sources:
        requested_sources = {"gdelt", "rss", "html"}

    config = load_config(args.config)
    data_dir = Path(config.get("storage", {}).get("data_dir", "/var/lib/world-state"))
    db_path = config.get("storage", {}).get("db_path", str(data_dir / "world_state.db"))
    init_db(db_path)

    session = request_session(config)
    articles: List[Dict[str, Any]] = []

    if "gdelt" in requested_sources:
        for topic in config.get("topics", []):
            try:
                articles.extend(fetch_gdelt(session, config, topic))
            except Exception as exc:
                print(f"gdelt fetch failed topic={topic.get('name')}: {exc}", file=sys.stderr)
            time.sleep(float(config.get("collector", {}).get("gdelt_request_delay_seconds", 6)))

    if "rss" in requested_sources:
        for feed in config.get("rss_feeds", []):
            try:
                articles.extend(fetch_rss(session, config, feed))
            except Exception as exc:
                print(f"rss fetch failed feed={feed.get('name')}: {exc}", file=sys.stderr)
            time.sleep(0.2)

    if "html" in requested_sources:
        for feed in config.get("html_feeds", []):
            try:
                articles.extend(fetch_html_feed(session, config, feed))
            except Exception as exc:
                print(f"html fetch failed feed={feed.get('name')}: {exc}", file=sys.stderr)
            time.sleep(0.2)

    inserted = store_articles(db_path, articles)
    state = build_state(db_path, config)
    alerts = detect_alerts(db_path, config)

    data_dir.mkdir(parents=True, exist_ok=True)
    write_json(data_dir / "world_state.json", state)
    append_jsonl(data_dir / "events.jsonl", [{"created_at": iso(now_utc()), "inserted": inserted, "fetched": len(articles)}])
    append_jsonl(data_dir / "alerts.jsonl", alerts)
    cleanup(db_path, config)

    print(json.dumps({"sources": sorted(requested_sources), "inserted": inserted, "fetched": len(articles), "alerts": len(alerts)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

