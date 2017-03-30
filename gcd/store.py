import logging
import random
import re
import zlib
import time
import pickle
import psycopg2
import json as json_

import threading as mt

from math import log
from itertools import combinations
from unittest import TestCase
from operator import attrgetter
from psycopg2.pool import ThreadedConnectionPool

from gcd.etc import identity, attrsetter, snippet
from gcd.chronos import span
from gcd.nix import sh
from gcd.work import Task


logger = logging.getLogger(__name__)


def execute(sql, args=(), cursor=None, values=False):
    return _execute('execute', sql, args, cursor, values)


def executemany(sql, args, cursor=None):
    return _execute('executemany', sql, args, cursor, False)


class Transaction:

    pool = None

    _local = mt.local()

    def active():
        return getattr(Transaction._local, 'active', None)

    def __init__(self, conn_or_pool=None):
        conn_or_pool = conn_or_pool or Transaction.pool
        self._pool = self._conn = None
        if hasattr(conn_or_pool, 'cursor'):
            self._conn = conn_or_pool
        else:
            self._pool = conn_or_pool

    def __enter__(self):
        active = Transaction.active()
        if active:
            return active
        if self._pool:
            self._conn = self._pool.acquire()
        self._cursors = []
        Transaction._local.active = self
        return self

    def cursor(self, *args, **kwargs):
        cursor = self._conn.cursor(*args, **kwargs)
        self._cursors.append(cursor)
        return cursor

    def __exit__(self, type_, value, traceback):
        active = Transaction.active()
        if active != self:
            return
        try:
            for cursor in self._cursors:
                try:
                    if not getattr(cursor, 'withhold', False):
                        cursor.close()
                except Exception:
                    pass  # Might have been legitimately closed by the user.
            if type_ is None:
                self._conn.commit()
            else:
                logger.error('Transaction rollback',
                             exc_info=(type_, value, traceback))
                self._conn.rollback()
        finally:
            Transaction._local.active = None
            if self._pool:
                self._pool.release(self._conn)
                self._conn = None


class Store:

    def __init__(self, conn_or_pool=None, create=True):
        self._conn_or_pool = conn_or_pool
        if create:
            with self.transaction():
                self._creation_lock()
                self._create()

    def transaction(self):
        return Transaction(self._conn_or_pool)

    def _create(self):
        raise NotImplementedError


class PgStore(Store):

    def _creation_lock(self):
        execute('SELECT pg_advisory_xact_lock(0)')


class PgConnectionPool:

    def __init__(self, *args, min_conns=1, keep_conns=10, max_conns=10,
                 **kwargs):
        self._pool = ThreadedConnectionPool(
            min_conns, max_conns, *args, **kwargs)
        self._keep_conns = keep_conns

    def acquire(self):
        pool = self._pool
        conn = pool.getconn()
        pool.minconn = min(self._keep_conns, len(pool._used))
        return conn

    def release(self, conn):
        self._pool.putconn(conn)

    def close(self):
        if hasattr(self, '_pool'):
            self._pool.closeall()

    __del__ = close


class PgVacuumer:

    semaphore = None

    def __init__(self, table, period=span(minutes=10),
                 size_period=span(minutes=1), full_size=None,
                 full_rate=0.01, semaphore=None, conn_or_pool=None):
        self._table = table
        self._period = period
        self._size_period = size_period
        self._full_size = full_size
        self._full_rate = full_rate
        self._full_next = 0
        self._semaphore = semaphore or self.semaphore
        self._conn_or_pool = conn_or_pool
        self._lock = mt.Lock()

    def start(self):
        self._size()
        Task(self._size_period, self._size).start()
        Task(self._period, self._vacuum).start()
        return self

    def auto(self, enable):
        with self._lock:
            enable = 'true' if enable else 'false'
            with Transaction(self._conn_or_pool):
                execute("""
                        ALTER TABLE %s SET (autovacuum_enabled = %s,
                                            toast.autovacuum_enabled = %s)
                        """ % (self._table, enable, enable))

    def _size(self):
        with Transaction(self._conn_or_pool):
            size, = next(
                execute('SELECT pg_relation_size(%s)', (self._table,)))
            self.too_big = self._full_size and size > self._full_size
        if self.too_big:
            now = time.time()
            if now > self._full_next:
                log = logging.getLogger('PgVacuumer')
                log.warning('Table %s is too big (%sb), running full vacuum.',
                            self._table, size)
                self._vacuum(full=True)
                self._full_next = now + (time.time() - now) / self._full_rate
                log.info('Table %s full vacuumed.', self._table)

    def _vacuum(self, full=False):
        with self._lock:
            self._semaphore and self._semaphore.acquire()
            try:
                with Transaction(self._conn_or_pool):
                    execute('END')  # End transaction opened by psycopg2.
                    execute('VACUUM %s %s' % (
                        'FULL' if full else 'ANALYZE', self._table))
            finally:
                self._semaphore and self._semaphore.release()


class PgFlattener:

    def __init__(self, obj_type=None, json=False, gzip=False):
        assert not (json and gzip)
        self.obj_type = obj_type
        self.col_type = 'jsonb' if json else 'bytea'

        if json:
            pair = json_.dumps, identity
        elif not gzip:
            pair = pickle.dumps, pickle.loads
        else:
            pair = (lambda obj: zlib.compress(pickle.dumps(obj)),
                    lambda col: pickle.loads(zlib.decompress(col)))
        self._dumps, self._loads = pair

        if obj_type is None:
            pair = identity, None
        elif hasattr(obj_type, '__getstate__'):
            pair = obj_type.__getstate__, obj_type.__setstate__
        else:
            pair = attrgetter('__dict__'), attrsetter('__dict__')
        self._get_state, self._set_state = pair

    def flatten(self, obj):
        return self._dumps(self._get_state(obj))

    def unflatten(self, col):
        state = self._loads(col)
        if self.obj_type:
            obj = self.obj_type.__new__(self.obj_type)
            self._set_state(obj, state)
            return obj
        else:
            return state


class PgTestCase(TestCase):

    db = 'test'

    def setUp(self):
        sh('dropdb --if-exists %s &> /dev/null' % self.db)
        sh('createdb %s' % self.db)

    def tearDown(self):
        # Try to kill it in bg because some conns might still be open.
        sh('dropdb %s &' % self.db)

    def connect(self, **kwargs):
        return psycopg2.connect(dbname=self.db, **kwargs)

    def pool(self, **kwargs):
        return PgConnectionPool(dbname=self.db, **kwargs)


def allot(seqs, base=None, capacity=None):
    assert not (base and capacity)
    if capacity is not None:
        assert capacity >= 1
        def quota(i):
            mem = 1 - 1 / capacity
            return (1 - mem**i) / (1 - mem)
    else:
        assert base >= 1
        def quota(i):
            return log(i, base or 1.5) + 1
    def score(seqs):
        return sum((quota(new_seq - s) - i)**2 for i, s in enumerate(seqs, 1))
    seqs = list(sorted(seqs, reverse=True))
    if not seqs:
        return 0, []
    new_seq = seqs[0] + 1
    num_seqs = min(len(seqs), round(quota(new_seq)))
    keep_seqs = min((c for c in combinations(seqs, num_seqs)), key=score)
    return new_seq, [seq for seq in seqs if seq not in keep_seqs]


def _execute(attr, sql, args, cursor, values):
    if cursor is None:
        cursor = Transaction.active().cursor()
    fun = getattr(cursor, attr)
    if values:
        sql, args = _values(sql, args)
    if logger.isEnabledFor(logging.DEBUG):
        _debugged(fun, sql, args)
    else:
        fun(sql, args)
    return cursor


def _values(sql, args):  # args can be any iterable.
    args_iter = iter(args)
    arg = next(args_iter)
    args = list(arg)
    args.extend(v for a in args_iter for v in a)
    value_sql = '(' + ','.join(['%s'] * len(arg)) + ')'
    values_sql = 'VALUES ' + ','.join([value_sql] * (len(args) // len(arg)))
    sql %= values_sql
    return sql, args


def _debugged(fun, sql, args):
    query_id = random.randint(0, 10000)
    log_sql = snippet(re.sub(r'[\n\t ]+', ' ', sql[:500]).strip(), 100)
    log_args = snippet(str(args[:20]), 100)
    logger.debug(dict(query=query_id, sql=log_sql, args=log_args))
    try:
        t0 = time.perf_counter()
        fun(sql, args)
        t1 = time.perf_counter()
        logger.debug(dict(query=query_id, time=t1 - t0))
    except:
        logger.exception(dict(query=query_id))
