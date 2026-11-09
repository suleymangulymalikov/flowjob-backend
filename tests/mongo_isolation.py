"""Start and verify a MongoDB process owned by this pytest invocation."""

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
from time import monotonic, sleep
from urllib.parse import quote
from uuid import uuid4

from pymongo import MongoClient
from pymongo.errors import PyMongoError


class IsolationError(Exception):
    """Safe to print. Never include configuration values or driver errors."""


@dataclass(frozen=True)
class TestMongo:
    directory: Path
    uri: str
    database: str


def require_mongod(environ: Mapping[str, str]) -> str:
    if {"MONGODB_URI", "MONGODB_DB_NAME"}.intersection(key.upper() for key in environ):
        raise IsolationError(
            "Unset MONGODB_URI and MONGODB_DB_NAME before running tests. "
            "External MongoDB configuration is forbidden, including localhost."
        )
    command = environ.get("FLOWJOB_TEST_MONGOD")
    if not command:
        raise IsolationError("Set FLOWJOB_TEST_MONGOD to mongod or the path to a local mongod executable.")
    executable = shutil.which(command)
    if executable is None:
        raise IsolationError("The FLOWJOB_TEST_MONGOD executable was not found or is not executable.")
    return str(Path(executable).resolve())


def verify_mongod(uri: str, pid: int, socket: Path, dbpath: Path) -> None:
    try:
        with MongoClient(uri, timeoutMS=3000) as client:
            options = client.admin.command("getCmdLineOpts")["parsed"]
            if (
                options["net"]["bindIp"] != str(socket)
                or options["net"]["unixDomainSocket"]["enabled"] is not False
                or options["storage"]["dbPath"] != str(dbpath)
                or client.admin.command("serverStatus")["pid"] != pid
            ):
                raise IsolationError("MongoDB did not match the disposable process.")
    except (PyMongoError, KeyError, TypeError):
        raise IsolationError("Could not verify the disposable MongoDB process.") from None


@contextmanager
def start_mongod(executable: str) -> Iterator[TestMongo]:
    # Unix sockets have short path limits. /tmp also avoids a custom TMPDIR on a shared mount.
    with TemporaryDirectory(prefix="flowjob-test-", dir="/tmp") as temporary:
        directory = Path(temporary).resolve()
        socket = directory / "mongo.sock"
        dbpath = directory / "db"
        dbpath.mkdir()
        # --nounixsocket disables the automatic public socket. --bind_ip supplies
        # the sole listener explicitly: a socket inside our private directory.
        try:
            process = subprocess.Popen(
                [executable, "--dbpath", str(dbpath), "--bind_ip", str(socket), "--nounixsocket"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            raise IsolationError("Could not launch the disposable MongoDB process.") from None
        try:
            deadline = monotonic() + 15
            while process.poll() is None and not socket.is_socket() and monotonic() < deadline:
                sleep(0.05)
            if process.poll() is not None or not socket.is_socket():
                raise IsolationError("MongoDB did not start on its private Unix socket.")
            uri = f"mongodb://{quote(str(socket), safe='')}/?directConnection=true"
            verify_mongod(uri, process.pid, socket, dbpath)
            yield TestMongo(directory, uri, f"flowjob_test_{uuid4().hex}")
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
