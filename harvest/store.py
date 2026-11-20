"""SQLite 持久化：目录观察、版本、覆盖窗口、订阅、标识关联与引用。

设计要点：
- observations 是只追加的来源证据；中断运行的部分页也保留，绝不抹掉；
- catalog_revisions 以内容哈希去重：同日更正（datestamp 不变、内容变化）
  产生新版本，边界日重复观察则不会；
- catalog_state 是最新观察的物化视图，删除标记与最后已知元数据分开保存；
- coverage_windows 只记录完整结束的运行所证明的来源范围，
  中断的运行不推进覆盖，也不清空其他订阅已发布的数据。
"""
import hashlib
import json
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  repo TEXT NOT NULL, set_spec TEXT NOT NULL, mode TEXT NOT NULL,
  from_day TEXT, until_day TEXT,
  started_at TEXT NOT NULL, finished_at TEXT,
  pages INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'running', note TEXT
);
CREATE TABLE IF NOT EXISTS observations(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL REFERENCES runs(id),
  page_no INTEGER NOT NULL,
  identifier TEXT NOT NULL, datestamp TEXT, deleted INTEGER NOT NULL DEFAULT 0,
  sets_json TEXT NOT NULL, record_xml TEXT NOT NULL, recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_obs_identifier ON observations(identifier);
CREATE TABLE IF NOT EXISTS catalog_revisions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  identifier TEXT NOT NULL, repo TEXT NOT NULL,
  datestamp TEXT, content_hash TEXT NOT NULL,
  deleted INTEGER NOT NULL DEFAULT 0, sets_json TEXT NOT NULL,
  title TEXT, relation TEXT,
  run_id INTEGER NOT NULL, recorded_at TEXT NOT NULL,
  UNIQUE(identifier, content_hash)
);
CREATE TABLE IF NOT EXISTS catalog_state(
  identifier TEXT PRIMARY KEY, repo TEXT NOT NULL,
  deleted INTEGER NOT NULL, sets_json TEXT NOT NULL, datestamp TEXT,
  title TEXT, relation TEXT, revision_id INTEGER NOT NULL, updated_run INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS detail_versions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  identifier TEXT NOT NULL, revision INTEGER, content_hash TEXT NOT NULL,
  payload_json TEXT NOT NULL, fetched_at TEXT NOT NULL,
  UNIQUE(identifier, content_hash)
);
CREATE TABLE IF NOT EXISTS detail_state(
  identifier TEXT PRIMARY KEY,
  revision INTEGER, metadata_public INTEGER, fulltext_open INTEGER, relation TEXT,
  fetch_status TEXT NOT NULL, last_http INTEGER, unverified_since TEXT, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fulltext_state(
  identifier TEXT PRIMARY KEY,
  status TEXT NOT NULL,
  http_status INTEGER, content_hash TEXT, content BLOB, open_at TEXT, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS coverage_windows(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  repo TEXT NOT NULL, set_spec TEXT NOT NULL,
  from_day TEXT NOT NULL, until_day TEXT NOT NULL,
  run_id INTEGER NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS subscriptions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  team TEXT NOT NULL, repo TEXT NOT NULL, set_spec TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL,
  UNIQUE(team, repo, set_spec)
);
CREATE TABLE IF NOT EXISTS identity_epochs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, cause TEXT
);
CREATE TABLE IF NOT EXISTS identity_members(
  epoch INTEGER NOT NULL, identifier TEXT NOT NULL, work_key TEXT NOT NULL,
  PRIMARY KEY(epoch, identifier)
);
CREATE TABLE IF NOT EXISTS identity_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  epoch INTEGER NOT NULL, identifier TEXT NOT NULL,
  from_work TEXT, to_work TEXT, cause TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS citations(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  team TEXT NOT NULL, identifier TEXT NOT NULL,
  work_key TEXT NOT NULL, catalog_revision_id INTEGER NOT NULL, detail_revision INTEGER,
  created_at TEXT NOT NULL
);
"""


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def content_hash(payload):
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self):
        self.db.close()

    def _one(self, sql, args=()):
        return self.db.execute(sql, args).fetchone()

    def _all(self, sql, args=()):
        return self.db.execute(sql, args).fetchall()

    # ---- 运行与覆盖 ----
    def start_run(self, repo, set_spec, mode, from_day):
        with self.db:
            cur = self.db.execute(
                'INSERT INTO runs(repo,set_spec,mode,from_day,started_at) VALUES(?,?,?,?,?)',
                (repo, set_spec, mode, from_day, utcnow()))
            return cur.lastrowid

    def finish_run(self, run_id, status, until_day=None, pages=0, note=None):
        with self.db:
            self.db.execute(
                'UPDATE runs SET status=?, until_day=?, pages=?, note=?, finished_at=? WHERE id=?',
                (status, until_day, pages, note, utcnow(), run_id))

    def runs_for(self, repo, set_spec):
        return [dict(r) for r in self._all(
            'SELECT * FROM runs WHERE repo=? AND set_spec=? ORDER BY id', (repo, set_spec))]

    def all_run_pairs(self):
        return [(r['repo'], r['set_spec']) for r in self._all(
            'SELECT DISTINCT repo,set_spec FROM runs ORDER BY repo,set_spec')]

    def add_coverage(self, repo, set_spec, from_day, until_day, run_id):
        with self.db:
            self.db.execute(
                'INSERT INTO coverage_windows(repo,set_spec,from_day,until_day,run_id,created_at)'
                ' VALUES(?,?,?,?,?,?)',
                (repo, set_spec, from_day, until_day, run_id, utcnow()))

    def coverage(self, repo, set_spec):
        return [dict(r) for r in self._all(
            'SELECT * FROM coverage_windows WHERE repo=? AND set_spec=? ORDER BY from_day,until_day',
            (repo, set_spec))]

    def coverage_pairs(self):
        return [(r['repo'], r['set_spec']) for r in self._all(
            'SELECT DISTINCT repo,set_spec FROM coverage_windows ORDER BY repo,set_spec')]

    def covered_upto(self, repo, set_spec):
        row = self._one(
            'SELECT MAX(until_day) AS upto FROM coverage_windows WHERE repo=? AND set_spec=?',
            (repo, set_spec))
        return row['upto'] if row else None

    def is_verified(self, repo, sets, datestamp):
        """成果的 datestamp 是否落在某个完整覆盖窗口内（区分完整覆盖与仍待核实）。"""
        if not datestamp or not sets:
            return False
        marks = ','.join('?' for _ in sets)
        row = self._one(
            f'SELECT 1 AS ok FROM coverage_windows WHERE repo=? AND set_spec IN ({marks})'
            ' AND from_day<=? AND until_day>=? LIMIT 1',
            (repo, *sets, datestamp, datestamp))
        return row is not None

    # ---- 目录观察与版本 ----
    def add_observation(self, run_id, page_no, rec):
        with self.db:
            self.db.execute(
                'INSERT INTO observations(run_id,page_no,identifier,datestamp,deleted,sets_json,'
                'record_xml,recorded_at) VALUES(?,?,?,?,?,?,?,?)',
                (run_id, page_no, rec['identifier'], rec['datestamp'], int(rec['deleted']),
                 json.dumps(rec['sets'], ensure_ascii=False), rec['record_xml'], utcnow()))

    def observations_for(self, identifier):
        return [dict(r) for r in self._all(
            'SELECT * FROM observations WHERE identifier=? ORDER BY id', (identifier,))]

    def first_deleted_observation(self, identifier):
        row = self._one(
            'SELECT * FROM observations WHERE identifier=? AND deleted=1 ORDER BY id LIMIT 1',
            (identifier,))
        return dict(row) if row else None

    def ensure_revision(self, run_id, repo, rec, digest):
        """按 (identifier, 内容哈希) 去重；同日更正产生新版本，重复观察不重复建档。"""
        row = self._one(
            'SELECT id FROM catalog_revisions WHERE identifier=? AND content_hash=?',
            (rec['identifier'], digest))
        if row:
            return row['id'], False
        with self.db:
            cur = self.db.execute(
                'INSERT INTO catalog_revisions(identifier,repo,datestamp,content_hash,deleted,'
                'sets_json,title,relation,run_id,recorded_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                (rec['identifier'], repo, rec['datestamp'], digest, int(rec['deleted']),
                 json.dumps(rec['sets'], ensure_ascii=False), rec['title'], rec['relation'],
                 run_id, utcnow()))
            return cur.lastrowid, True

    def revisions_for(self, identifier):
        return [dict(r) for r in self._all(
            'SELECT * FROM catalog_revisions WHERE identifier=? ORDER BY id', (identifier,))]

    def upsert_state(self, repo, rec, revision_id, run_id):
        """最新观察物化视图；删除时保留最后已知元数据（关联解释需要历史标识）。"""
        with self.db:
            self.db.execute(
                'INSERT INTO catalog_state(identifier,repo,deleted,sets_json,datestamp,title,'
                'relation,revision_id,updated_run) VALUES(?,?,?,?,?,?,?,?,?)'
                ' ON CONFLICT(identifier) DO UPDATE SET'
                ' deleted=excluded.deleted, sets_json=excluded.sets_json,'
                ' datestamp=excluded.datestamp,'
                ' title=COALESCE(excluded.title,catalog_state.title),'
                ' relation=COALESCE(excluded.relation,catalog_state.relation),'
                ' revision_id=excluded.revision_id, updated_run=excluded.updated_run',
                (rec['identifier'], repo, int(rec['deleted']),
                 json.dumps(rec['sets'], ensure_ascii=False), rec['datestamp'],
                 rec['title'], rec['relation'], revision_id, run_id))

    def state(self, identifier):
        row = self._one('SELECT * FROM catalog_state WHERE identifier=?', (identifier,))
        if not row:
            return None
        data = dict(row)
        data['sets'] = json.loads(data.pop('sets_json'))
        return data

    def all_state(self):
        rows = []
        for r in self._all('SELECT * FROM catalog_state ORDER BY identifier'):
            data = dict(r)
            data['sets'] = json.loads(data.pop('sets_json'))
            rows.append(data)
        return rows

    # ---- 详情版本 ----
    def ensure_detail_version(self, identifier, payload, digest):
        row = self._one(
            'SELECT id FROM detail_versions WHERE identifier=? AND content_hash=?',
            (identifier, digest))
        if row:
            return row['id'], False
        with self.db:
            cur = self.db.execute(
                'INSERT INTO detail_versions(identifier,revision,content_hash,payload_json,'
                'fetched_at) VALUES(?,?,?,?,?)',
                (identifier, payload.get('revision'), digest,
                 json.dumps(payload, ensure_ascii=False), utcnow()))
            return cur.lastrowid, True

    def detail_versions_for(self, identifier):
        return [dict(r) for r in self._all(
            'SELECT * FROM detail_versions WHERE identifier=? ORDER BY id', (identifier,))]

    def first_nonpublic_detail(self, identifier):
        for row in self.detail_versions_for(identifier):
            if not json.loads(row['payload_json']).get('metadata_public', True):
                return row
        return None

    def set_detail_ok(self, identifier, payload):
        with self.db:
            self.db.execute(
                'INSERT INTO detail_state(identifier,revision,metadata_public,fulltext_open,'
                'relation,fetch_status,last_http,unverified_since,updated_at)'
                " VALUES(?,?,?,?,?,'ok',200,NULL,?)"
                ' ON CONFLICT(identifier) DO UPDATE SET'
                ' revision=excluded.revision, metadata_public=excluded.metadata_public,'
                ' fulltext_open=excluded.fulltext_open, relation=excluded.relation,'
                " fetch_status='ok', last_http=200, unverified_since=NULL, updated_at=excluded.updated_at",
                (identifier, payload.get('revision'),
                 int(bool(payload.get('metadata_public'))),
                 int(bool(payload.get('fulltext_open'))),
                 payload.get('relation'), utcnow()))

    def set_detail_unverified(self, identifier, http_status):
        """详情暂时不可用：保留最后已知版本，只标记未核实，不当作撤回。"""
        with self.db:
            self.db.execute(
                'INSERT INTO detail_state(identifier,fetch_status,last_http,unverified_since,'
                'updated_at) VALUES(?,?,?,?,?)'
                ' ON CONFLICT(identifier) DO UPDATE SET'
                " fetch_status='unverified', last_http=excluded.last_http,"
                ' unverified_since=COALESCE(detail_state.unverified_since,excluded.unverified_since),'
                ' updated_at=excluded.updated_at',
                (identifier, 'unverified', http_status, utcnow(), utcnow()))

    def detail_state(self, identifier):
        row = self._one('SELECT * FROM detail_state WHERE identifier=?', (identifier,))
        return dict(row) if row else None

    # ---- 原件领取 ----
    def set_fulltext(self, identifier, status, http_status, content=None, open_at=None):
        digest = hashlib.sha256(content).hexdigest() if content is not None else None
        with self.db:
            self.db.execute(
                'INSERT INTO fulltext_state(identifier,status,http_status,content_hash,content,'
                'open_at,updated_at) VALUES(?,?,?,?,?,?,?)'
                ' ON CONFLICT(identifier) DO UPDATE SET'
                ' status=excluded.status, http_status=excluded.http_status,'
                ' content_hash=COALESCE(excluded.content_hash,fulltext_state.content_hash),'
                ' content=COALESCE(excluded.content,fulltext_state.content),'
                ' open_at=COALESCE(excluded.open_at,fulltext_state.open_at),'
                ' updated_at=excluded.updated_at',
                (identifier, status, http_status, digest, content, open_at, utcnow()))

    def clear_fulltext_content(self, identifier):
        with self.db:
            self.db.execute(
                "UPDATE fulltext_state SET content=NULL,status='pruned',updated_at=?"
                ' WHERE identifier=?', (utcnow(), identifier))

    def fulltext(self, identifier):
        row = self._one('SELECT * FROM fulltext_state WHERE identifier=?', (identifier,))
        return dict(row) if row else None

    def fulltext_all(self):
        return [dict(r) for r in self._all('SELECT * FROM fulltext_state ORDER BY identifier')]

    # ---- 订阅 ----
    def subscribe(self, team, repo, set_spec):
        with self.db:
            self.db.execute(
                'INSERT INTO subscriptions(team,repo,set_spec,active,updated_at) VALUES(?,?,?,1,?)'
                ' ON CONFLICT(team,repo,set_spec) DO UPDATE SET active=1, updated_at=excluded.updated_at',
                (team, repo, set_spec, utcnow()))

    def unsubscribe(self, team, repo, set_spec):
        """仅停用订阅；不动任何已采集数据，共享原件由其他有效订阅继续引用。"""
        with self.db:
            self.db.execute(
                'UPDATE subscriptions SET active=0, updated_at=? WHERE team=? AND repo=? AND set_spec=?',
                (utcnow(), team, repo, set_spec))

    def subscriptions(self, team=None, active_only=True):
        sql = 'SELECT * FROM subscriptions'
        cond, args = [], []
        if team:
            cond.append('team=?')
            args.append(team)
        if active_only:
            cond.append('active=1')
        if cond:
            sql += ' WHERE ' + ' AND '.join(cond)
        return [dict(r) for r in self._all(sql + ' ORDER BY id', args)]

    def active_pairs(self):
        return [(r['repo'], r['set_spec']) for r in self._all(
            'SELECT repo,set_spec FROM subscriptions WHERE active=1'
            ' GROUP BY repo,set_spec ORDER BY MIN(id)')]

    # ---- 标识关联（仅来源给出的 relation）----
    def latest_epoch(self):
        row = self._one('SELECT MAX(id) AS epoch FROM identity_epochs')
        return row['epoch'] if row and row['epoch'] is not None else None

    def new_epoch(self, membership, cause):
        with self.db:
            cur = self.db.execute(
                'INSERT INTO identity_epochs(created_at,cause) VALUES(?,?)', (utcnow(), cause))
            epoch = cur.lastrowid
            self.db.executemany(
                'INSERT INTO identity_members(epoch,identifier,work_key) VALUES(?,?,?)',
                [(epoch, ident, work) for ident, work in membership.items()])
            return epoch

    def membership(self, epoch=None):
        epoch = epoch or self.latest_epoch()
        if epoch is None:
            return {}
        return {r['identifier']: r['work_key'] for r in self._all(
            'SELECT identifier,work_key FROM identity_members WHERE epoch=?', (epoch,))}

    def add_identity_events(self, epoch, events, cause):
        with self.db:
            self.db.executemany(
                'INSERT INTO identity_events(epoch,identifier,from_work,to_work,cause,created_at)'
                ' VALUES(?,?,?,?,?,?)',
                [(epoch, e['identifier'], e['from_work'], e['to_work'], cause, utcnow())
                 for e in events])

    def identity_events_for(self, identifier):
        return [dict(r) for r in self._all(
            'SELECT * FROM identity_events WHERE identifier=? ORDER BY id', (identifier,))]

    # ---- 引用 ----
    def add_citation(self, team, identifier, work_key, catalog_revision_id, detail_revision):
        with self.db:
            cur = self.db.execute(
                'INSERT INTO citations(team,identifier,work_key,catalog_revision_id,'
                'detail_revision,created_at) VALUES(?,?,?,?,?,?)',
                (team, identifier, work_key, catalog_revision_id, detail_revision, utcnow()))
            return cur.lastrowid

    def citation(self, citation_id):
        row = self._one('SELECT * FROM citations WHERE id=?', (citation_id,))
        return dict(row) if row else None

    def citations(self, team=None):
        if team:
            return [dict(r) for r in self._all(
                'SELECT * FROM citations WHERE team=? ORDER BY id', (team,))]
        return [dict(r) for r in self._all('SELECT * FROM citations ORDER BY id')]
