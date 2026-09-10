"""SQLite 저장소 — 서치콘솔·GA4 수집값과 동기화 기록."""
import os
import sqlite3
from contextlib import contextmanager

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get('SEO_DASH_DB', os.path.join(BASE, 'data', 'seo.db'))

SCHEMA = """
CREATE TABLE IF NOT EXISTS gsc_daily (
  site TEXT, date TEXT, clicks INTEGER, impressions INTEGER, ctr REAL, position REAL,
  PRIMARY KEY (site, date));
CREATE TABLE IF NOT EXISTS gsc_query (
  site TEXT, run_date TEXT, window TEXT, query TEXT, clicks INTEGER, impressions INTEGER, position REAL,
  PRIMARY KEY (site, run_date, window, query));
CREATE TABLE IF NOT EXISTS gsc_page (
  site TEXT, run_date TEXT, window TEXT, page TEXT, clicks INTEGER, impressions INTEGER, position REAL,
  PRIMARY KEY (site, run_date, window, page));
CREATE TABLE IF NOT EXISTS gsc_sitemap (
  site TEXT, run_date TEXT, path TEXT, submitted INTEGER, indexed INTEGER, last_downloaded TEXT, errors INTEGER, warnings INTEGER,
  PRIMARY KEY (site, run_date, path));
CREATE TABLE IF NOT EXISTS ga_daily (
  property TEXT, name TEXT, date TEXT, users INTEGER, pageviews INTEGER, sessions INTEGER,
  PRIMARY KEY (property, date));
CREATE TABLE IF NOT EXISTS ga_page (
  property TEXT, run_date TEXT, page TEXT, pageviews INTEGER, users INTEGER,
  PRIMARY KEY (property, run_date, page));
CREATE TABLE IF NOT EXISTS ga_host (
  property TEXT, run_date TEXT, host TEXT, pageviews INTEGER, users INTEGER, sessions INTEGER,
  PRIMARY KEY (property, run_date, host));
CREATE TABLE IF NOT EXISTS ga_source (
  property TEXT, run_date TEXT, source TEXT, sessions INTEGER,
  PRIMARY KEY (property, run_date, source));
CREATE TABLE IF NOT EXISTS sync_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, started TEXT, finished TEXT, status TEXT, message TEXT);
CREATE TABLE IF NOT EXISTS naver_daily (
  site TEXT, date TEXT, clicks INTEGER, impressions INTEGER, visitors INTEGER,
  PRIMARY KEY (site, date));
CREATE TABLE IF NOT EXISTS idx_url (
  site TEXT, url TEXT, sitemap TEXT, first_seen TEXT, naver_at TEXT, bing_at TEXT,
  PRIMARY KEY (site, url));
CREATE TABLE IF NOT EXISTS idx_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, site TEXT, kind TEXT, engine TEXT, count INTEGER, status TEXT, message TEXT);
CREATE TABLE IF NOT EXISTS idx_sample (
  site TEXT, date TEXT, total INTEGER, indexed INTEGER, crawled INTEGER, discovered INTEGER, unknown INTEGER, other INTEGER,
  PRIMARY KEY (site, date));
"""


def connect():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


@contextmanager
def tx():
    con = connect()
    try:
        yield con
        con.commit()
    finally:
        con.close()
