"""Python-owned file login store. Public projections never contain bearer values."""

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import stat
import secrets


class StoreError(RuntimeError):
    """Closed messages only: filesystem/provider values must not reach diagnostics."""


class StoreContentError(StoreError):
    """A private, regular store exists but its contents cannot be used."""


@dataclass(frozen=True, repr=False)
class Secret:
    _value: str

    def reveal(self):
        return self._value

    def __str__(self):
        return "***"

    __repr__ = __str__


@dataclass(frozen=True)
class Login:
    identity: str = ""
    user_id: Secret = field(default_factory=lambda: Secret(""))
    user_key: Secret = field(default_factory=lambda: Secret(""))
    domain: str = ""
    generation: int = 0
    saved_at: str = ""
    password: Secret = field(default_factory=lambda: Secret(""))
    origin: str = "none"
    notice: str = ""

    def public(self):
        return {
            "configured": self.origin != "none",
            "identity": self.identity,
            "origin": self.origin,
            "backend": "file",
            "expiry": "unknown",
            "generation": self.generation,
            "notice": self.notice,
        }


def path():
    try:
        root = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local/state")
        if not Path(root).is_absolute() or "\0" in root:
            raise StoreError("Login state directory must be absolute")
        root = os.path.realpath(root)
    except (OSError, RuntimeError) as error:
        if isinstance(error, StoreError):
            raise
        raise StoreError("Login state root could not be resolved") from None
    return Path(root) / "zlibrary/logins/zlibrary.json"


def _check(fd, directory=False):
    info = os.fstat(fd)
    expected = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    mode = 0o700 if directory else 0o600
    if (
        not expected
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != mode
        or (not directory and info.st_nlink != 1)
    ):
        raise StoreError(
            "Login store permissions must be private (0700 directories, 0600 files)"
        )


@contextmanager
def _directory(create=False):
    """Traverse using directory descriptors so a path swap cannot follow a link."""
    fd = None
    try:
        if os.name != "posix":
            raise StoreError("File login storage requires POSIX private permissions")
        # The state root belongs to the caller: its ancestors may be symlinks.
        # Only project-owned components have no-follow and private-mode rules.
        root = path().parents[2]
        if create:
            root.mkdir(parents=True, mode=0o700, exist_ok=True)
        try:
            fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        except FileNotFoundError:
            yield None
            return
        for part in ("zlibrary", "logins"):
            try:
                child = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
                )
            except FileNotFoundError:
                if not create:
                    yield None
                    return
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                child = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
                )
            os.close(fd)
            fd = child
            _check(fd, directory=True)
        yield fd
    except OSError:
        raise StoreError("Login store path could not be accessed") from None
    finally:
        if fd is not None:
            os.close(fd)


def _read(fd, *, tolerate_content=False):
    if fd is None:
        return None
    try:
        handle = os.open(
            "zlibrary.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd
        )
    except FileNotFoundError:
        return None
    try:
        _check(handle)
        with os.fdopen(handle, "r", closefd=False) as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ValueError()
        data = json.loads(raw)
        if (
            not isinstance(data, dict)
            or set(data)
            != {
                "version",
                "source",
                "identity",
                "user_id",
                "user_key",
                "domain",
                "saved_at",
                "generation",
            }
            or type(data["version"]) is not int
            or data["version"] != 1
            or data["source"] != "zlibrary"
            or type(data["generation"]) is not int
            or data["generation"] < 1
            or any(
                not isinstance(data[k], str) or not data[k]
                for k in ("identity", "user_id", "user_key", "domain", "saved_at")
            )
        ):
            raise ValueError()
        try:
            _domain(data["domain"])
        except StoreError:
            raise ValueError() from None
        return Login(
            identity=data["identity"],
            user_id=Secret(data["user_id"]),
            user_key=Secret(data["user_key"]),
            domain=data["domain"],
            saved_at=data["saved_at"],
            generation=data["generation"],
            origin="saved",
        )
    except (ValueError, TypeError):
        if tolerate_content:
            return None
        raise StoreContentError(
            "Login store content is invalid; run zlibrary login or logout"
        ) from None
    finally:
        os.close(handle)


def _domain(value):
    # Host allow-list is deferred; reject URL syntax, paths, controls and userinfo.
    import re

    if not re.fullmatch(
        r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?(?::[0-9]{1,5})?", value
    ):
        raise StoreError("Login domain is invalid")


def load(*, tolerate_content=False):
    if os.name != "posix":
        return None
    with _directory() as fd:
        return _read(fd, tolerate_content=tolerate_content)


@contextmanager
def _locked():
    import fcntl

    with _directory(create=True) as directory:
        lock = os.open(
            ".zlibrary.lock",
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
            dir_fd=directory,
        )
        try:
            _check(lock)
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield directory, lock
        finally:
            os.close(lock)


def save(*, identity, user_id, user_key, domain):
    _domain(domain)
    if any(
        not isinstance(v, str) or not v or len(v) > 8192
        for v in (identity, user_id, user_key)
    ):
        raise StoreError("Login response is invalid")
    with _locked() as (directory, lock):
        previous = _read(directory, tolerate_content=True)
        try:
            raw = os.read(lock, 1048577)
            if len(raw) > 1048576 or (raw and not raw.endswith(b"\n")):
                raise ValueError()
            counters = [int(line) for line in raw.splitlines()]
            if any(value < 0 for value in counters):
                raise ValueError()
            counter = max(counters, default=0)
        except ValueError:
            raise StoreError("Login generation record is invalid") from None
        generation = max(counter, previous.generation if previous else 0) + 1
        data = dict(
            version=1,
            source="zlibrary",
            identity=identity,
            user_id=user_id,
            user_key=user_key,
            domain=domain,
            saved_at=datetime.now(timezone.utc).isoformat(),
            generation=generation,
        )
        # Reserve the generation in the persistent lock, including across logout.
        # Append rather than truncate: a failed reservation must not erase the
        # last generation after logout. Partial records are refused on next use.
        os.lseek(lock, 0, os.SEEK_END)
        reservation = f"{generation}\n".encode()
        if os.write(lock, reservation) != len(reservation):
            raise StoreError("Login generation could not be reserved")
        os.fsync(lock)
        name = ".login-" + secrets.token_hex(12)
        handle = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory,
        )
        try:
            with os.fdopen(handle, "w") as stream:
                json.dump(data, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(
                name, "zlibrary.json", src_dir_fd=directory, dst_dir_fd=directory
            )
            os.fsync(directory)
        finally:
            try:
                os.unlink(name, dir_fd=directory)
            except FileNotFoundError:
                pass
        return _read(directory)


def logout():
    with _locked() as (directory, _):
        _read(directory, tolerate_content=True)  # Safety checks still apply.
        try:
            os.unlink("zlibrary.json", dir_fd=directory)
        except FileNotFoundError:
            pass
        else:
            os.fsync(directory)


def resolve(env=None):
    env = os.environ if env is None else env
    email, password = env.get("ZLIBRARY_EMAIL", ""), env.get("ZLIBRARY_PASSWORD", "")
    environment = bool(email and password)
    problem = ""
    try:
        saved = load()
    except StoreContentError:
        saved = None
        problem = "Saved login store content is invalid; run zlibrary login to replace it or zlibrary logout to remove it."
    except StoreError:
        saved = None
        problem = "Saved login store is unavailable or unsafe; check its permissions and owned paths."
    if problem:
        return Login(
            identity=email if environment else "",
            password=Secret(password) if environment else Secret(""),
            origin="environment" if environment else "none",
            notice=problem
            + (
                " Using ZLIBRARY_EMAIL and ZLIBRARY_PASSWORD."
                if environment
                else " No account is configured."
            ),
        )
    if environment and (saved is None or env.get("ZLIBRARY_PREFER_ENV") == "1"):
        return Login(
            identity=email,
            password=Secret(password),
            origin="environment",
            notice="Using ZLIBRARY_PASSWORD because ZLIBRARY_PREFER_ENV=1"
            if saved
            else "",
        )
    if saved:
        if environment:
            from dataclasses import replace

            saved = replace(
                saved, notice="Saved login takes precedence over ZLIBRARY_PASSWORD"
            )
        return saved
    return Login()


if __name__ == "__main__":
    try:
        print(json.dumps(resolve().public()))
    except StoreError:
        print(
            json.dumps(
                {
                    "error": "Login store refused; check private permissions and run zlibrary login"
                }
            )
        )
        raise SystemExit(1) from None
