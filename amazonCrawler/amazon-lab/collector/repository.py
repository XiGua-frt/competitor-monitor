"""SQLite is the durable source of truth for tasks, observations and controls."""
import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


class LeaseLost(ValueError):
    pass


def profile_key(config, asin=None):
    values = {k:config[k] for k in ('marketplace','delivery_postcode','currency','language','price_scope','variant_policy')}
    values['variant'] = config.get('tracked_variants', {}).get(asin) if asin else None
    return hashlib.sha256(encode(values).encode()).hexdigest()[:24]


class Repository:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        version = self.db.execute('PRAGMA user_version').fetchone()[0]
        if version not in (0,1):
            self.db.close()
            raise ValueError('unsupported_database_schema')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS products (
          asin TEXT, profile TEXT, first_seen REAL, last_seen REAL, metadata TEXT,
          PRIMARY KEY(asin,profile));
        CREATE TABLE IF NOT EXISTS tasks (
          id TEXT PRIMARY KEY, kind TEXT, entity TEXT, profile TEXT, payload TEXT,
          state TEXT DEFAULT 'pending', due_at REAL, created_at REAL, attempts INTEGER DEFAULT 0,
          owner TEXT, lease_until REAL, reason TEXT);
        CREATE UNIQUE INDEX IF NOT EXISTS active_task ON tasks(kind,entity,profile)
          WHERE state IN ('pending','running');
        CREATE TABLE IF NOT EXISTS snapshots (
          id TEXT PRIMARY KEY, asin TEXT, profile TEXT, observation_key TEXT, run_id TEXT,
          observed_at TEXT, observed_epoch REAL, saved_at REAL, data TEXT,
          UNIQUE(asin,profile,observation_key));
        CREATE INDEX IF NOT EXISTS history ON snapshots(asin,profile,observed_epoch);
        CREATE TABLE IF NOT EXISTS discoveries (
          run_id TEXT, position INTEGER, data TEXT, PRIMARY KEY(run_id,position));
        CREATE TABLE IF NOT EXISTS requests (
          id TEXT PRIMARY KEY, run_id TEXT, task_id TEXT, reserved_at REAL,
          outcome TEXT DEFAULT 'reserved', http_status INTEGER);
        CREATE INDEX IF NOT EXISTS request_time ON requests(reserved_at);
        CREATE TABLE IF NOT EXISTS runs (
          id TEXT PRIMARY KEY, started_at REAL, mode TEXT, finished_at REAL, summary TEXT);
        CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);
        PRAGMA user_version=1;
        ''')

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
        except BaseException:
            self.db.rollback()
            raise
        else:
            self.db.commit()

    def state(self, key, default=None):
        row = self.db.execute('SELECT value FROM state WHERE key=?',(key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_state(self, key, value):
        self.db.execute('INSERT INTO state VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                        (key,encode(value)))

    def discover(self, asin, profile, metadata, now):
        self.db.execute('INSERT INTO products VALUES (?,?,?,?,?) ON CONFLICT(asin,profile) DO UPDATE SET last_seen=excluded.last_seen, metadata=excluded.metadata',
                        (asin,profile,now,now,encode(metadata)))

    def products(self):
        return [dict(r) for r in self.db.execute('SELECT * FROM products ORDER BY first_seen,asin')]

    def enqueue(self, kind, entity, profile, payload, now, due_at=None):
        with self.transaction():
            return self._enqueue(kind,entity,profile,payload,now,due_at)

    def _enqueue(self,kind,entity,profile,payload,now,due_at=None):
        row = self.db.execute("SELECT id FROM tasks WHERE kind=? AND entity=? AND profile=? AND state IN ('pending','running')",
                              (kind,entity,profile)).fetchone()
        if row:
            return row[0]
        if self.state('hold:'+kind+':'+profile+':'+entity,0)>now:
            return None
        task_id = uuid.uuid4().hex
        self.db.execute('INSERT INTO tasks(id,kind,entity,profile,payload,due_at,created_at) VALUES (?,?,?,?,?,?,?)',
                        (task_id,kind,entity,profile,encode(payload),now if due_at is None else due_at,now))
        return task_id

    def tasks(self):
        return [self._task(r) for r in self.db.execute('SELECT * FROM tasks ORDER BY created_at,id')]

    @staticmethod
    def _task(row):
        data = dict(row)
        data['payload'] = json.loads(data['payload'])
        return data

    def claim(self, owner, now, lease_seconds=300):
        with self.transaction():
            self.db.execute("UPDATE tasks SET state='pending',owner=NULL,lease_until=NULL WHERE state='running' AND lease_until<=?",(now,))
            row = self.db.execute("SELECT * FROM tasks WHERE state='pending' AND due_at<=? ORDER BY CASE kind WHEN 'search' THEN 0 ELSE 1 END,created_at,id LIMIT 1",(now,)).fetchone()
            if not row:
                return None
            self.db.execute("UPDATE tasks SET state='running',owner=?,lease_until=? WHERE id=?",(owner,now+lease_seconds,row['id']))
            data = self._task(row)
            data.update(owner=owner,lease_until=now+lease_seconds,state='running')
            return data

    def _owns(self, task):
        row = self.db.execute("SELECT * FROM tasks WHERE id=? AND state='running' AND owner=?",(task['id'],task['owner'])).fetchone()
        if not row:
            raise LeaseLost('task_lease_lost')
        return row

    def finish_task(self, task, state='done', reason=None, due_at=None, now=None):
        with self.transaction():
            self._owns(task)
            if now is not None and not self.valid_leases(task,now):
                raise LeaseLost('worker_lease_lost')
            self.db.execute('UPDATE tasks SET state=?,reason=?,due_at=COALESCE(?,due_at),owner=NULL,lease_until=NULL WHERE id=?',
                            (state,reason,due_at,task['id']))
            if state=='failed' and due_at is not None:
                self.set_state('hold:'+task['kind']+':'+task['profile']+':'+task['entity'],due_at)

    def complete_snapshot(self, task, row, run_id, now):
        with self.transaction():
            self._owns(task)
            if not self.valid_leases(task,now):
                raise LeaseLost('worker_lease_lost')
            snapshot_id = self._save_snapshot(row,task['profile'],'task:'+task['id'],run_id,now)
            self.db.execute("UPDATE tasks SET state='done',owner=NULL,lease_until=NULL,reason=NULL WHERE id=?",(task['id'],))
            return snapshot_id

    def save_snapshot(self, row, profile, observation_key, run_id, now):
        with self.transaction():
            return self._save_snapshot(row,profile,observation_key,run_id,now)

    def _save_snapshot(self, row, profile, observation_key, run_id, now):
        timestamp = datetime.fromisoformat(row['fetched_at'])
        if timestamp.tzinfo is None:
            raise ValueError('observation_timestamp_requires_timezone')
        asin = row['external_id']
        snapshot_id = uuid.uuid5(uuid.NAMESPACE_URL,encode([asin,profile,observation_key])).hex
        self.db.execute('INSERT OR IGNORE INTO snapshots VALUES (?,?,?,?,?,?,?,?,?)',
                        (snapshot_id,asin,profile,observation_key,run_id,row['fetched_at'],timestamp.timestamp(),now,encode(row)))
        self.discover(asin,profile,{'title':row.get('title'),'relevance':row.get('relevance')},now)
        return snapshot_id

    def snapshots(self, asin=None, profile=None):
        query, params = 'SELECT * FROM snapshots WHERE 1=1', []
        for name, value in (('asin',asin),('profile',profile)):
            if value is not None:
                query += ' AND '+name+'=?'
                params.append(value)
        query += ' ORDER BY observed_epoch,id'
        return [{**dict(r),'data':json.loads(r['data'])} for r in self.db.execute(query,params)]

    def latest(self, asin, profile):
        row = self.db.execute('SELECT data FROM snapshots WHERE asin=? AND profile=? ORDER BY observed_epoch DESC,id DESC LIMIT 1',(asin,profile)).fetchone()
        return json.loads(row[0]) if row else None

    def is_fresh(self, asin, profile, now, refresh_seconds):
        row = self.db.execute('SELECT MAX(observed_epoch) FROM snapshots WHERE asin=? AND profile=?',(asin,profile)).fetchone()
        return row[0] is not None and now < row[0]+refresh_seconds

    def last_valid_price(self, asin, profile):
        for record in reversed(self.snapshots(asin,profile)):
            commerce = record['data'].get('commerce',{})
            if commerce.get('price') is not None:
                return {'price':commerce['price'],'currency':commerce.get('currency'), 'observed_at':record['observed_at']}
        return None

    def save_discoveries(self, run_id, records):
        with self.transaction():
            self.db.executemany('INSERT OR IGNORE INTO discoveries VALUES (?,?,?)',
                               [(run_id,r['returned_position'],encode(r)) for r in records])

    def acquire_worker(self, owner, now, lease_seconds=300):
        with self.transaction():
            value = self.state('worker',{})
            if value.get('owner') not in (None,owner) and value.get('until',0)>now:
                return False
            self.set_state('worker',{'owner':owner,'until':now+lease_seconds})
            return True

    def release_worker(self, owner):
        with self.transaction():
            if self.state('worker',{}).get('owner')==owner:
                self.set_state('worker',{})

    def valid_leases(self, task, now):
        row=self.db.execute("SELECT owner,lease_until FROM tasks WHERE id=? AND state='running'",(task['id'],)).fetchone()
        if not row or row['owner']!=task['owner'] or row['lease_until']<=now:
            return False
        if task['owner'].startswith('worker:'):
            worker=self.state('worker',{})
            return worker.get('owner')==task['owner'] and worker.get('until',0)>now
        return True

    def scheduler_due(self, key, now, interval):
        with self.transaction():
            value = self.state('schedule:'+key,0)
            if value>now:
                return False
            self.set_state('schedule:'+key,now+interval)
            return True

    def schedule_search(self,keyword,profile,now,interval):
        key='schedule:search:'+profile+':'+keyword
        with self.transaction():
            if self.state(key,0)>now:
                return None
            task_id=self._enqueue('search',keyword,profile,{'keyword':keyword},now)
            self.set_state(key,now+interval)
            return task_id

    def start_run(self, run_id, now, mode):
        self.db.execute('INSERT INTO runs(id,started_at,mode) VALUES (?,?,?)',(run_id,now,mode))

    def finish_run(self, run_id, now, summary):
        self.db.execute('UPDATE runs SET finished_at=?,summary=? WHERE id=?',(now,encode(summary),run_id))

    def request_counts(self, now, run_id=None):
        return {'hour':self.db.execute('SELECT COUNT(*) FROM requests WHERE reserved_at>?',(now-3600,)).fetchone()[0],
                'day':self.db.execute('SELECT COUNT(*) FROM requests WHERE reserved_at>?',(now-86400,)).fetchone()[0],
                'run':self.db.execute('SELECT COUNT(*) FROM requests WHERE run_id=?',(run_id,)).fetchone()[0] if run_id else 0}

    def reserve_request(self, run_id, task, now, limits):
        with self.transaction():
            if task and not self.valid_leases(task,now):
                return {'blocked':'worker_lease_lost'}
            state = self.state('guard',{})
            if state.get('paused'):
                return {'blocked':'paused','reason':state.get('pause_reason')}
            if state.get('cooldown_until',0)>now:
                return {'blocked':'cooldown','until':state['cooldown_until']}
            counts = self.request_counts(now,run_id)
            for name,seconds,limit in (('run',None,limits['max_requests_per_run']),
                                      ('hour',3600,limits['max_requests_per_hour']),
                                      ('day',86400,limits['max_requests_per_day'])):
                if counts[name]>=limit:
                    oldest = (self.db.execute('SELECT MIN(reserved_at) FROM requests WHERE reserved_at>?',(now-seconds,)).fetchone()[0]
                              if seconds else None)
                    return {'blocked':name+'_budget_exhausted','until':oldest+seconds if seconds else None}
            if state.get('next_request_at',0)>now:
                return {'blocked':'request_interval','until':state['next_request_at']}
            if task:
                self._owns(task)
                self.db.execute('UPDATE tasks SET attempts=attempts+1 WHERE id=?',(task['id'],))
                task['attempts'] += 1
            request_id = uuid.uuid4().hex
            self.db.execute('INSERT INTO requests(id,run_id,task_id,reserved_at) VALUES (?,?,?,?)',
                            (request_id,run_id,task['id'] if task else None,now))
            state['next_request_at'] = now+limits['min_interval_seconds']
            self.set_state('guard',state)
            return {'request_id':request_id}

    def request_outcome(self, request_id, outcome, status=None):
        self.db.execute('UPDATE requests SET outcome=?,http_status=? WHERE id=?',(outcome,status,request_id))

    def pause(self, reason):
        with self.transaction():
            state=self.state('guard',{})
            state.update(paused=True,pause_reason=reason)
            self.set_state('guard',state)

    def resume(self):
        with self.transaction():
            state=self.state('guard',{})
            state.update(paused=False,pause_reason=None,consecutive_errors=0)
            # Resuming never clears request usage, next-request time or cooldown.
            self.set_state('guard',state)
