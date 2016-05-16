import os
import sys
import signal
import subprocess
import fcntl
import argparse

from contextlib import contextmanager


env = os.environ
path = os.path
exit = sys.exit
argv = sys.argv


def sh(cmd, input=None):
    if not isinstance(cmd, str):
        cmd = cmd[0] % tuple(sh_quote(arg) for arg in cmd[1:])
    cmd = cmd.strip()
    stdin = stdout = stderr = None
    if input is not None:
        if not isinstance(input, str):
            input = '\n'.join(input)
        stdin = subprocess.PIPE
    if cmd[-1] == '|':
        stdout = stderr = subprocess.PIPE
    proc = subprocess.Popen(
        cmd.rstrip('&|'), shell=True, universal_newlines=True,
        stdin=stdin, stdout=stdout, stderr=stderr)
    if cmd[-1] in '&':
        if input is not None:
            proc.stdin.write(input)
        return proc
    else:
        output, error = proc.communicate(input)
        if proc.returncode != 0 or error:
            raise ShError(proc.returncode, cmd, output, error)
        else:
            return output and output.strip('\n')


def sh_quote(text, quote="'"):
    if quote == "'":
        return "'%s'" % text.replace("'", r"'\''")
    elif quote == '"':
        return '"%s"' % text.replace('"', r'\"')
    else:
        raise ValueError('Unknown quote %s', quote)


def sh_expand(expr):
    return sh('echo %s|' % expr)


class ShError(subprocess.CalledProcessError):
    pass


def cat(path):
    with(open(path)) as file:
        return file.read().strip('\n')


@contextmanager
def flock(path):
    with open(path, 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


@contextmanager
def cwd(path):
    prev = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(prev)


def kill_me(sig=signal.SIGKILL):
    os.kill(os.getpid(), sig)


def kill_us(sig=signal.SIGKILL):
    os.kill(0, sig)


def killable(killer=kill_us):
    signal.signal(signal.SIGINT, killer)
    signal.signal(signal.SIGTERM, killer)


class Command:

    def __init__(self):
        self._top = self._cur = argparse.ArgumentParser()
        self._sub = self._args = None

    def arg(self, *args, **kwargs):
        self._cur.add_argument(*args, **kwargs)

    def sub(self, fun):
        if self._sub is None:
            self._sub = self._top.add_subparsers(dest='cmd')
            self._sub.required = True
        try:
            self._cur = self._sub.add_parser(fun.__name__, help=fun.__doc__)
            gen = fun()
            next(gen)
            self._cur.set_defaults(_gen=gen)
        finally:
            self._cur = self._top

    @property
    def args(self):
        if self._args is None:
            self._args = self._top.parse_args()
        return self._args

    def run(self, fun):
        self._top.description = fun.__doc__
        fun()
        if '_gen' in self.args:
            try:
                next(self._args._gen)
            except StopIteration:
                pass

cmd = Command()
