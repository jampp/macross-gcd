import logging
import multiprocessing as mp
import threading as mt

from queue import Empty, Queue

from gcd.etc import new
from gcd.chronos import as_timer


logger = logging.getLogger(__name__)

default_hwm = 10000

default_period = 1


class Process(mp.Process):

    init = None

    def __init__(self, target, *args, daemon=True, **kwargs):
        super().__init__(
            target=self._wrapper,
            daemon=daemon,
            args=(self.init, target, *args),
            kwargs=kwargs,
        )

    def start(self):
        super().start()
        return self

    @staticmethod  # Must be pickleable.
    def _wrapper(init, target, *args, **kwargs):
        if init:
            Process.init = init  # Recursively propagate init.
            init()
        target(*args, **kwargs)


class Thread(mt.Thread):
    def __init__(self, target, *args, daemon=True, **kwargs):
        super().__init__(target=target, daemon=daemon, args=args, kwargs=kwargs)

    def start(self):
        super().start()
        return self


class Worker:
    def __init__(self, *args, new_process=False, **kwargs):
        worker_class = Process if new_process else Thread
        self.worker = worker_class(*args, *kwargs)

    def start(self):
        self.worker.start()
        return self

    def join(self):
        self.worker.join()


class Task(Worker):
    @new
    class Stop:
        pass

    def __init__(self, period_or_timer, callback, *args, new_process=False, **kwargs):
        timer = as_timer(period_or_timer)
        self.__stop = False
        super().__init__(
            self._run, timer, callback, args, kwargs, new_process=new_process
        )

    def stop(self):
        self.__stop = True
        return self

    def _run(self, timer, callback, args, kwargs):
        while True:
            try:
                timer.wait()
                if self.__stop or callback(*args, **kwargs) is Task.Stop:
                    logger.info("Task cleanly stopped")
                    return
            except Exception:
                logger.exception("Error executing task")


class Batcher(Task):
    def __init__(
        self,
        handle_batch,
        *args,
        hwm=None,
        period=None,
        queue=None,
        new_process=False,
        **kwargs
    ):
        self._queue = queue or new_queue(hwm, new_process)
        super().__init__(
            period or default_period,
            self._callback,
            handle_batch,
            args,
            kwargs,
            new_process=new_process,
        )

    def put(self, obj, *args, **kwargs):
        self._queue.put(obj, *args, **kwargs)

    def join(self):
        self._queue.put(Task.Stop)
        super().join()

    def _callback(self, handle_batch, args, kwargs):
        batch = list(dequeue(self._queue, 1))
        stop = batch[-1] is Task.Stop
        handle_batch(batch[:-1] if stop else batch, *args, **kwargs)
        if stop:
            return Task.Stop


class Streamer(Task):
    def __init__(
        self,
        load_batch,
        *args,
        hwm=None,
        period=None,
        queue=None,
        new_process=False,
        **kwargs
    ):
        self._queue = queue or new_queue(hwm, new_process)
        super().__init__(
            period or default_period,
            self._callback,
            load_batch,
            self._queue.maxsize,
            period,
            args,
            kwargs,
            new_process=new_process,
        )

    def get(self, *args, **kwargs):
        return self._queue.get(*args, **kwargs)

    def __iter__(self):
        return self

    def __next__(self):
        obj = self._queue.get()
        if obj is Task.Stop:
            raise StopIteration
        return obj

    def _callback(self, load_batch, hwm, period, args, kwargs):
        for obj in load_batch(hwm, period, *args, **kwargs):
            self._queue.put(obj)
            if obj is Task.Stop:
                return Task.Stop


def new_queue(hwm=None, shared=False, pack=1):
    queue_class = mp.Queue if shared else Queue
    return queue_class(int((hwm or default_hwm) / pack))


def dequeue(queue, at_least=0, at_most=None):
    at_most = at_most or queue.qsize()
    for _ in range(at_least):
        yield queue.get()
    try:
        for _ in range(at_most - at_least):
            yield queue.get_nowait()
    except Empty:
        pass


def packer(put, size):
    def wrapper(*obj, flush=False):
        nonlocal pack
        pack.extend(obj)
        if flush or len(pack) >= size:
            put(pack)
            pack = []

    pack = []
    return wrapper


def unpacker(get):
    def wrapper():
        nonlocal pack
        if not pack:
            pack = get()
        return pack.pop(0)

    pack = None
    return wrapper
