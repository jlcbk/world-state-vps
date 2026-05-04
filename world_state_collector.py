#!/usr/bin/env python3
import argparse
import email.utils
import hashlib
import json
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlparse

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
    for item in items[:100]:
        title = text_of(item, "title")
        link = text_of(item, "link")
        if not link:
            atom_link = item.find("{http://www.w3.org/2005/Atom}link")
            link = atom_link.attrib.get("href", "") if atom_link is not None else ""
        published = text_of(item, "pubDate") or text_of(item, "published") or text_of(item, "updated")
        desc = text_of(item, "description") or text_of(item, "summary")
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
                "snippet": clean_text(desc)[:500],
                "raw": {"feed": feed["name"]},
            }
        )
    return out


def text_of(node: ET.Element, name: str) -> str:
    found = node.find(name)
    if found is None:
        found = node.find("{http://www.w3.org/2005/Atom}" + name)
    return clean_text(found.text if found is not None and found.text else "")


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
    all_topics = sorted(set(topics + rss_topics))

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
    args = parser.parse_args()

    config = load_config(args.config)
    data_dir = Path(config.get("storage", {}).get("data_dir", "/var/lib/world-state"))
    db_path = config.get("storage", {}).get("db_path", str(data_dir / "world_state.db"))
    init_db(db_path)

    session = request_session(config)
    articles: List[Dict[str, Any]] = []

    for topic in config.get("topics", []):
        try:
            articles.extend(fetch_gdelt(session, config, topic))
        except Exception as exc:
            print(f"gdelt fetch failed topic={topic.get('name')}: {exc}", file=sys.stderr)
        time.sleep(0.5)

    for feed in config.get("rss_feeds", []):
        try:
            articles.extend(fetch_rss(session, config, feed))
        except Exception as exc:
            print(f"rss fetch failed feed={feed.get('name')}: {exc}", file=sys.stderr)
        time.sleep(0.2)

    inserted = store_articles(db_path, articles)
    state = build_state(db_path, config)
    alerts = detect_alerts(db_path, config)

    data_dir.mkdir(parents=True, exist_ok=True)
    write_json(data_dir / "world_state.json", state)
    append_jsonl(data_dir / "events.jsonl", [{"created_at": iso(now_utc()), "inserted": inserted, "fetched": len(articles)}])
    append_jsonl(data_dir / "alerts.jsonl", alerts)
    cleanup(db_path, config)

    print(json.dumps({"inserted": inserted, "fetched": len(articles), "alerts": len(alerts)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

