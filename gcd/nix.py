import os
import sys
import signal
import subprocess
import fcntl
import argparse

from contextlib import contextmanager
from types import GeneratorType

from gcd.etc import as_file


env = os.environ
path = os.path
exit = sys.exit
argv = sys.argv


def sh(cmd, input=None):
    cmd = as_cmd(cmd)
    stdin = stdout = stderr = None
    if input is not None:
        if not isinstance(input, str):
            input = "\n".join(input)
        stdin = subprocess.PIPE
    if cmd[-1] == "|" or cmd[-2] == "|":
        stdout = stderr = subprocess.PIPE
    # Make sure `cmd` has strict POSIX-compatible syntax (no bashisms)
    proc = subprocess.Popen(
        cmd.rstrip("&|"),
        shell=True,
        universal_newlines=True,
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
    )
    if cmd[-1] == "&" or cmd[-2] == "&":
        if input is not None:
            proc.stdin.write(input)
            proc.stdin.close()  # Avoid deadlock when reading stdout
        return proc
    else:
        output, error = proc.communicate(input)
        if proc.returncode != 0 or error:
            raise ShError(proc.returncode, cmd, output, error)
        else:
            return output and output.strip("\n")


def as_cmd(cmd):
    if not isinstance(cmd, str):
        cmd = cmd[0] % tuple(sh_quote(arg) for arg in cmd[1:])
    return cmd.strip()


def sh_quote(text, quote="'"):
    if quote == "'":
        return "'%s'" % text.replace("'", r"'\''")
    elif quote == '"':
        return '"%s"' % text.replace('"', r"\"")
    else:
        raise ValueError("Unknown quote %s", quote)


def sh_expand(expr):
    return sh("echo %s|" % expr)


class ShError(subprocess.CalledProcessError):
    pass


def cat(path):
    with (open(path)) as file:
        return file.read().strip("\n")


@contextmanager
def flock(file_or_path, mode="a", shared=False):
    with as_file(file_or_path, mode) as file:
        fcntl.flock(file, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
        try:
            yield file
        finally:
            fcntl.flock(file, fcntl.LOCK_UN)


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


def make_killable(killer=kill_us):
    signal.signal(signal.SIGINT, lambda *args: killer())
    signal.signal(signal.SIGTERM, lambda *args: killer())


class Command:
    def __init__(self):
        self._top = self._cur = argparse.ArgumentParser()
        self._sub = self._args = None

    def arg(self, *args, **kwargs):
        self._cur.add_argument(*args, **kwargs)

    @property
    def args(self):
        if self._args is None:
            self._args = self._top.parse_args()
        return self._args

    def sub(self, fun, *, name=None, doc=None):
        if self._sub is None:
            self._sub = self._top.add_subparsers(dest="cmd")
            self._sub.required = True
        try:
            self._cur = self._sub.add_parser(
                name or fun.__name__, help=doc or fun.__doc__
            )
            gen = fun()
            next(gen)  # Run first part of sub cmd.
            self._cur.set_defaults(_gen=gen)
        finally:
            self._cur = self._top

    def run(self, fun=None, *, doc=None):
        if sys._getframe(1).f_globals["__name__"] != "__main__":
            return
        self._top.description = doc or fun.__doc__
        if fun:
            ret = fun()
            if isinstance(ret, GeneratorType):
                # Allow for sub cmds to be also top cmds.
                next(ret)
                ret = next(ret, None)
        if "_gen" in self.args:  # Run second part of sub cmd.
            ret = next(self.args._gen, None)
        if ret is not None:
            sys.exit(ret)


cmd = Command()
