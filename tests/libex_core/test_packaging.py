"""
The built distribution holds exactly what is meant to ship, and says so.

The wheel and sdist are built for real and every archive member is checked
against an explicit allowlist, because the failure being guarded is silent: a
packaging backend sweeps in whatever sits beside the package, and nothing at
build time says a stray file came along. Members are read from the archives
without extracting them, so a hostile or malformed name is inspected as a
string rather than acted on.
"""

import configparser
import importlib.util
import re
import subprocess
import sys
import tarfile
import zipfile
from email import message_from_bytes
from pathlib import Path, PurePosixPath

import pytest

import libex_core

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_DIR = REPO_ROOT / "libex_core"

# Built with --no-isolation so the build never reaches for a package index;
# that needs the backend and the frontend already present in the environment.
_BUILD_TOOLS = ("build", "flit_core")
_MISSING = [m for m in _BUILD_TOOLS if importlib.util.find_spec(m) is None]

pytestmark = pytest.mark.skipif(
    bool(_MISSING),
    reason=f"packaging build tools not installed: {', '.join(_MISSING)}",
)

_DIST_INFO = f"libex_core-{libex_core.__version__}.dist-info"
_SDIST_ROOT = f"libex_core-{libex_core.__version__}"

_PACKAGE_FILES = {
    path.relative_to(REPO_ROOT).as_posix()
    for path in PACKAGE_DIR.rglob("*")
    if path.is_file()
    and "__pycache__" not in path.parts
    and path.suffix != ".pyc"
}

# flit ships every non-bytecode file inside the package directory, so the
# package's own changelog travels in the wheel, and so does the template the
# storage migrations are written from. Those are the only non-Python files
# allowed to.
_PACKAGE_ALLOWED_SUFFIXES = {".py"}
_PACKAGE_ALLOWED_NAMES = {"py.typed", "CHANGELOG.md", "script.py.mako"}

# The man page and completions are installed under the environment prefix by
# flit's external-data, which puts them under the wheel's .data/data/ tree.
# Exactly these four names, never a directory glob: a fifth file in the data
# directory, or a .data/scripts entry that an installer would execute or
# place on PATH, must fail here rather than ship.
_DATA_DIR = f"libex_core-{libex_core.__version__}.data"
_DATA_SHARE_PATHS = (
    "share/man/man1/libex-core.1",
    "share/bash-completion/completions/libex-core",
    "share/zsh/site-functions/_libex-core",
    "share/fish/vendor_completions.d/libex-core.fish",
)
_WHEEL_DATA_ALLOWED = {f"{_DATA_DIR}/data/{path}" for path in _DATA_SHARE_PATHS}
_SDIST_DATA_ALLOWED = {
    f"{_SDIST_ROOT}/libex-core-data/{path}" for path in _DATA_SHARE_PATHS
}
_DATA_SCHEMES_FORBIDDEN = re.compile(r"\.data/(scripts|purelib|platlib|headers)(/|$)")
_DATA_DIRECTORY_PATTERN = r"\.data(/|$)"

_ENTRY_POINT = ("libex-core", "libex_core.cli.main:main")

_WHEEL_METADATA_ALLOWED = {
    f"{_DIST_INFO}/entry_points.txt",
    f"{_DIST_INFO}/WHEEL",
    f"{_DIST_INFO}/METADATA",
    f"{_DIST_INFO}/RECORD",
    f"{_DIST_INFO}/licenses/LICENSE",
}

_SDIST_ROOT_ALLOWED = {
    f"{_SDIST_ROOT}/LICENSE",
    f"{_SDIST_ROOT}/PYPI.md",
    f"{_SDIST_ROOT}/pyproject.toml",
    f"{_SDIST_ROOT}/PKG-INFO",
}

_FORBIDDEN_PATTERNS = (
    re.compile(r"(^|/)__pycache__(/|$)"),
    re.compile(r"\.py[co]$"),
    re.compile(r"(^|/)\.env"),
    re.compile(r"(^|/)app(/|$)"),
    re.compile(r"(^|/)tests(/|$)"),
    re.compile(_DATA_DIRECTORY_PATTERN),
)

_EXPECTED_REQUIRES_DIST = {
    "httpx>=0.28.1,<0.29",
    "pydantic>=2.13.4,<3",
}

# Optional extras, spelled out here rather than read from pyproject.toml so a
# dependency added there without being declared here fails the build check.
_EXPECTED_EXTRAS = {
    "storage": {
        "sqlalchemy>=2.0.46,<2.1",
        "alembic>=1.18.4,<1.19",
        "aiosqlite>=0.22.1,<0.23",
    },
    "postgres": {
        "libex-core[storage]",
        "asyncpg>=0.31,<0.32",
    },
    "socks": {
        "httpx[socks]>=0.28.1,<0.29",
    },
}
_EXTRA_MARKER = re.compile(r'^extra == "([a-z0-9-]+)"$')


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    outdir = tmp_path_factory.mktemp("dist")
    result = subprocess.run(
        [sys.executable, "-m", "build", "--no-isolation", "--outdir", str(outdir), str(REPO_ROOT)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    wheels = list(outdir.glob("*.whl"))
    sdists = list(outdir.glob("*.tar.gz"))
    assert len(wheels) == 1 and len(sdists) == 1
    return wheels[0], sdists[0]


def _wheel_names(wheel):
    with zipfile.ZipFile(wheel) as zf:
        return zf.namelist()


def _sdist_members(sdist):
    with tarfile.open(sdist) as tf:
        return tf.getmembers()


def _data_names(names):
    return {name for name in names if re.search(_DATA_DIRECTORY_PATTERN, name)}


def _assert_safe_name(name):
    path = PurePosixPath(name)
    assert not path.is_absolute(), name
    assert ".." not in path.parts, name
    assert "\\" not in name, name
    for pattern in _FORBIDDEN_PATTERNS:
        # The .data ban stands for every name except the four allowed
        # verbatim; a near miss such as a fifth file beside them still trips it.
        if pattern.pattern == _DATA_DIRECTORY_PATTERN and name in _WHEEL_DATA_ALLOWED:
            continue
        assert not pattern.search(name), f"{name} matches {pattern.pattern}"


def _assert_package_member(name):
    path = PurePosixPath(name)
    assert path.parts[0] == "libex_core", name
    assert path.name in _PACKAGE_ALLOWED_NAMES or path.suffix in _PACKAGE_ALLOWED_SUFFIXES, name


def test_wheel_contains_only_the_allowlist(built):
    wheel, _ = built
    names = _wheel_names(wheel)
    for name in names:
        _assert_safe_name(name)
        if name.startswith("libex_core/"):
            _assert_package_member(name)
        else:
            assert name in _WHEEL_METADATA_ALLOWED | _WHEEL_DATA_ALLOWED, name
    assert {n for n in names if n.startswith("libex_core/")} == _PACKAGE_FILES
    assert _WHEEL_METADATA_ALLOWED <= set(names)


def test_wheel_ships_the_typing_marker(built):
    wheel, _ = built
    assert "libex_core/py.typed" in _wheel_names(wheel)


def test_sdist_contains_only_the_allowlist(built):
    _, sdist = built
    members = _sdist_members(sdist)
    for member in members:
        assert member.isreg(), f"{member.name} is not a regular file"
        _assert_safe_name(member.name)
        if member.name.startswith(f"{_SDIST_ROOT}/libex_core/"):
            _assert_package_member(member.name.removeprefix(f"{_SDIST_ROOT}/"))
        else:
            assert member.name in _SDIST_ROOT_ALLOWED | _SDIST_DATA_ALLOWED, member.name
    names = {m.name for m in members}
    assert _SDIST_ROOT_ALLOWED <= names
    assert _SDIST_DATA_ALLOWED <= names
    assert {n.removeprefix(f"{_SDIST_ROOT}/") for n in names if "/libex_core/" in n} == _PACKAGE_FILES


def test_wheel_has_no_symlinks_or_special_entries(built):
    wheel, _ = built
    with zipfile.ZipFile(wheel) as zf:
        for info in zf.infolist():
            assert not info.is_dir(), info.filename
            mode = info.external_attr >> 16
            assert (mode & 0o170000) in (0, 0o100000), f"{info.filename} is not a regular file"


def _metadata(wheel):
    with zipfile.ZipFile(wheel) as zf:
        return message_from_bytes(zf.read(f"{_DIST_INFO}/METADATA"))


def test_metadata_version_matches_the_package(built):
    wheel, _ = built
    assert _metadata(wheel)["Version"] == libex_core.__version__


def test_metadata_name_and_python_floor(built):
    wheel, _ = built
    meta = _metadata(wheel)
    assert meta["Name"] == "libex-core"
    assert meta["Requires-Python"] == ">=3.12"
    assert "Typing :: Typed" in meta.get_all("Classifier")


def _split_requirements(message):
    """Split Requires-Dist into the unconditional set and a per-extra map."""
    unconditional, extras = set(), {}
    for line in message.get_all("Requires-Dist") or []:
        requirement, _, marker = (part.strip() for part in line.partition(";"))
        if not marker:
            unconditional.add(requirement)
            continue
        match = _EXTRA_MARKER.match(marker)
        assert match, f"{line!r} carries a marker that is not a bare extra"
        extras.setdefault(match.group(1), set()).add(requirement)
    return unconditional, extras


def _sdist_pkg_info(sdist):
    with tarfile.open(sdist) as tf:
        return message_from_bytes(tf.extractfile(f"{_SDIST_ROOT}/PKG-INFO").read())


def test_runtime_dependencies_are_exactly_the_two(built):
    """The unconditional requirements are httpx and pydantic and nothing else;
    anything else must sit behind an extra marker."""
    wheel, sdist = built
    for message in (_metadata(wheel), _sdist_pkg_info(sdist)):
        unconditional, _ = _split_requirements(message)
        assert unconditional == _EXPECTED_REQUIRES_DIST


def test_extras_carry_exactly_the_declared_requirements(built):
    wheel, sdist = built
    for message in (_metadata(wheel), _sdist_pkg_info(sdist)):
        _, extras = _split_requirements(message)
        assert extras == _EXPECTED_EXTRAS


def test_no_extras_exist_beyond_the_declared_ones(built):
    wheel, sdist = built
    for message in (_metadata(wheel), _sdist_pkg_info(sdist)):
        assert set(message.get_all("Provides-Extra")) == set(_EXPECTED_EXTRAS)


def test_the_requirement_split_is_not_inert():
    """A requirement planted without a marker must land in the unconditional
    set, and a marker that is not a bare extra must be refused."""
    planted = message_from_bytes(
        b"Requires-Dist: httpx>=1\nRequires-Dist: sqlalchemy>=2 ; extra == \"storage\"\n"
        b"Requires-Dist: evil>=1\n"
    )
    unconditional, extras = _split_requirements(planted)
    assert unconditional == {"httpx>=1", "evil>=1"}
    assert extras == {"storage": {"sqlalchemy>=2"}}
    bad = message_from_bytes(b'Requires-Dist: x ; python_version >= "3"\n')
    with pytest.raises(AssertionError):
        _split_requirements(bad)


def test_long_description_is_the_package_readme(built):
    wheel, sdist = built
    readme = (REPO_ROOT / "PYPI.md").read_text(encoding="utf-8")
    meta = _metadata(wheel)
    assert meta["Description-Content-Type"] == "text/markdown"
    assert meta.get_payload().strip() == readme.strip()
    with tarfile.open(sdist) as tf:
        pkg_info = message_from_bytes(tf.extractfile(f"{_SDIST_ROOT}/PKG-INFO").read())
    assert pkg_info["Description-Content-Type"] == "text/markdown"
    assert pkg_info.get_payload().strip() == readme.strip()


# ============================================================
# DATA FILES -- the man page and completions, and nothing beside them
# ============================================================

def test_wheel_data_tree_is_exactly_the_four_files(built):
    wheel, _ = built
    assert _data_names(_wheel_names(wheel)) == _WHEEL_DATA_ALLOWED


def test_wheel_installs_no_scripts_purelib_platlib_or_headers(built):
    """A .data/scripts entry is copied onto PATH and made executable by an
    installer, and purelib/platlib/headers write into the interpreter's own
    trees. None of them is something this package asks for."""
    wheel, _ = built
    for name in _wheel_names(wheel):
        assert not _DATA_SCHEMES_FORBIDDEN.search(name), name


@pytest.mark.parametrize(
    "scheme", ["scripts", "purelib", "platlib", "headers"]
)
def test_the_forbidden_scheme_pattern_catches_each_scheme(scheme):
    """Proves the pattern above is not inert: it must match a planted name for
    every scheme it claims to forbid, and must not match the allowed data."""
    assert _DATA_SCHEMES_FORBIDDEN.search(f"{_DATA_DIR}/{scheme}/anything")
    assert _DATA_SCHEMES_FORBIDDEN.search(f"{_DATA_DIR}/{scheme}")
    assert not any(_DATA_SCHEMES_FORBIDDEN.search(n) for n in _WHEEL_DATA_ALLOWED)


def test_the_data_ban_still_rejects_a_fifth_data_file():
    """The .data pattern is kept, with the four names as the only exemption."""
    for planted in (
        f"{_DATA_DIR}/data/share/man/man1/other.1",
        f"{_DATA_DIR}/data/share/libex-core-extra",
        f"{_DATA_DIR}/scripts/libex-core",
        f"{_DATA_DIR}/data/{_DATA_SHARE_PATHS[0]}.bak",
    ):
        with pytest.raises(AssertionError):
            _assert_safe_name(planted)
    for allowed in _WHEEL_DATA_ALLOWED:
        _assert_safe_name(allowed)


def test_the_data_files_in_the_wheel_and_sdist_are_the_committed_ones(built):
    wheel, sdist = built
    with zipfile.ZipFile(wheel) as zf, tarfile.open(sdist) as tf:
        for relative in _DATA_SHARE_PATHS:
            committed = (REPO_ROOT / "libex-core-data" / relative).read_bytes()
            assert zf.read(f"{_DATA_DIR}/data/{relative}") == committed
            member = tf.extractfile(f"{_SDIST_ROOT}/libex-core-data/{relative}")
            assert member.read() == committed


def test_sdist_data_tree_is_exactly_the_four_sources(built):
    _, sdist = built
    names = {m.name for m in _sdist_members(sdist)}
    sources = {n for n in names if n.startswith(f"{_SDIST_ROOT}/libex-core-data")}
    assert sources == _SDIST_DATA_ALLOWED


# ============================================================
# ENTRY POINT -- the one console script
# ============================================================

def test_entry_points_is_exactly_the_one_console_script(built):
    wheel, _ = built
    with zipfile.ZipFile(wheel) as zf:
        text = zf.read(f"{_DIST_INFO}/entry_points.txt").decode()
    parser = configparser.ConfigParser(delimiters=("=",))
    parser.optionxform = str
    parser.read_string(text)
    assert parser.sections() == ["console_scripts"]
    assert parser.items("console_scripts") == [_ENTRY_POINT]


# ============================================================
# MAN PAGE AND COMPLETIONS -- text the installer places for the shell to read
# ============================================================

# Requests that read a file, run a command, or write one. A man page that
# carries any of them can disclose or execute something when it is rendered.
_ROFF_FORBIDDEN_REQUESTS = frozenset(
    {"so", "mso", "pso", "sy", "pi", "open", "opena", "write", "cf"}
)
_ROFF_REQUEST = re.compile(r"^[.'][ \t]*([A-Za-z]+)", re.MULTILINE)


def _roff_forbidden(text):
    return sorted(
        {m.group(1) for m in _ROFF_REQUEST.finditer(text)} & _ROFF_FORBIDDEN_REQUESTS
    )


def _committed(relative):
    return (REPO_ROOT / "libex-core-data" / relative).read_text(encoding="utf-8")


@pytest.mark.parametrize("request_name", sorted(_ROFF_FORBIDDEN_REQUESTS))
def test_the_roff_check_catches_each_forbidden_request(request_name):
    for lead in (".", "'", ". ", ".\t"):
        assert _roff_forbidden(f".TH X 1\n{lead}{request_name} /etc/passwd\n") == [
            request_name
        ]
    assert _roff_forbidden(".SH NAME\nthe word so mid-line\n") == []


def test_man_page_has_no_file_or_command_requests(built):
    wheel, _ = built
    with zipfile.ZipFile(wheel) as zf:
        shipped = zf.read(f"{_DATA_DIR}/data/{_DATA_SHARE_PATHS[0]}").decode()
    assert _roff_forbidden(shipped) == []
    assert _roff_forbidden(_committed(_DATA_SHARE_PATHS[0])) == []
    assert shipped.startswith(".TH "), "the man page check read something else"


_COMPLETIONS = {
    "bash": _DATA_SHARE_PATHS[1],
    "zsh": _DATA_SHARE_PATHS[2],
    "fish": _DATA_SHARE_PATHS[3],
}

# Variables a completion script may expand. The shell's own completion state
# and the script's own locals; never HOME, PATH or anything the user set.
_ALLOWED_VARIABLES = {
    "bash": {"COMP_WORDS", "COMP_CWORD", "cur", "cmd", "ci", "i", "word", "candidates"},
    "zsh": {"curcontext", "state", "line", "@"},
    "fish": set(),
}
_VARIABLE_REFERENCE = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*|[@*#?!$0-9-])")

# What may begin a statement. Anything else is a command this package did not
# mean the shell to run.
_ALLOWED_COMMANDS = {
    "bash": {
        "local", "for", "do", "done", "case", "esac", "if", "then", "else", "fi",
        "break", "complete", "COMPREPLY=()", "}",
    },
    "zsh": {
        "local", "typeset", "case", "esac", "_arguments", "_describe",
        "_libex-core", "}", "commands=(",
    },
}
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\+?=")
_CASE_ARM = re.compile(r"^\(?[A-Za-z0-9_\"*|-]*\)\s*")
_ARITHMETIC = re.compile(r"\(\([^)]*\)\)")
# A condition is one subcommand test, or a group word and then the command
# inside it: `A; and B` or `A; and not B`, each a __fish_seen_subcommand_from
# over plain words. No other builtin, no substitution, no third term.
_WORDS = r"[a-z-]+(?: [a-z-]+)*"
_SEEN = rf"__fish_seen_subcommand_from {_WORDS}"
_FISH_CONDITION = re.compile(
    rf"-n ('{_SEEN}(?:; and (?:not )?{_SEEN})?'|__fish_use_subcommand)( |$)"
)


def _statement_heads(text):
    heads = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Quoted continuation lines of a multi-line argument list.
        if line[0] in "'{)(" or line == ";;":
            continue
        line = _ARITHMETIC.sub("((", line)
        arm = _CASE_ARM.match(line)
        if arm:
            line = line[arm.end():]
        for statement in re.split(r";|&&|\|\|", line):
            statement = statement.strip()
            if statement:
                heads.append(statement.split()[0])
    return heads


def _completion_problems(shell, text):
    problems = []
    if "$(" in text:
        problems.append("command substitution $(")
    if "`" in text:
        problems.append("backtick")
    if re.search(r"\beval\b", text):
        problems.append("eval")
    for forbidden in ("|", "<(", ">(", "&>", " >", "<<"):
        if forbidden in text:
            problems.append(f"shell operator {forbidden!r}")
    for name in _VARIABLE_REFERENCE.findall(text):
        if name not in _ALLOWED_VARIABLES[shell]:
            problems.append(f"expands ${name}")
    for line in text.splitlines():
        if "libex-core" in line and not line.lstrip().startswith("#"):
            allowed = (
                line == "complete -F _libex_core libex-core"
                or line == "#compdef libex-core"
                or line.startswith("complete -c libex-core ")
                or "'libex-core command'" in line
                or line.startswith("_libex-core()")
                or line == '_libex-core "$@"'
            )
            if not allowed:
                problems.append(f"calls back into libex-core: {line.strip()}")
    if shell == "fish":
        for line in text.splitlines():
            if line and not line.startswith(("#", "complete -c libex-core ")):
                problems.append(f"fish line that is not a complete registration: {line}")
            if "(" in line:
                problems.append(f"fish command substitution: {line}")
            if " -n " in line and not _FISH_CONDITION.search(line):
                problems.append(f"fish condition that is not a subcommand test: {line}")
    else:
        for head in _statement_heads(text):
            if head in _ALLOWED_COMMANDS[shell] or _ASSIGNMENT.match(head):
                continue
            if shell == "bash" and head in {"_libex_core()", "[[", "((", "("}:
                continue
            if shell == "zsh" and head.startswith("_libex-core()"):
                continue
            problems.append(f"runs {head!r}")
    return problems


@pytest.mark.parametrize("shell", sorted(_COMPLETIONS))
def test_committed_completions_hold_no_execution_or_environment_reads(shell):
    text = _committed(_COMPLETIONS[shell])
    assert text.strip(), "an empty file would pass every check below"
    assert _completion_problems(shell, text) == []


@pytest.mark.parametrize("shell", sorted(_COMPLETIONS))
def test_shipped_completions_are_checked_too(built, shell):
    wheel, _ = built
    with zipfile.ZipFile(wheel) as zf:
        text = zf.read(f"{_DATA_DIR}/data/{_COMPLETIONS[shell]}").decode()
    assert _completion_problems(shell, text) == []


@pytest.mark.parametrize(
    ("shell", "planted", "expected"),
    [
        ("bash", 'x=$(libex-core config)', "command substitution"),
        ("bash", "x=`id`", "backtick"),
        ("bash", 'eval "$cur"', "eval"),
        ("bash", 'echo "$HOME"', "expands $HOME"),
        ("bash", 'echo "${PATH}"', "expands $PATH"),
        ("bash", "libex-core config", "calls back into libex-core"),
        ("bash", "curl http://example.invalid", "runs 'curl'"),
        ("bash", "cat /etc/passwd | sh", "shell operator"),
        ("zsh", 'x=$(python -c 1)', "command substitution"),
        ("zsh", 'print "$AUDIBLE_PROXY_URL"', "expands $AUDIBLE_PROXY_URL"),
        ("zsh", "libex-core config", "calls back into libex-core"),
        ("zsh", "wget http://example.invalid", "runs 'wget'"),
        ("fish", "complete -c libex-core -a '(libex-core config)'", "command substitution"),
        ("fish", "complete -c libex-core -n 'test -f /etc/passwd' -a x", "fish condition"),
        ("fish", "libex-core config", "calls back into libex-core"),
        ("fish", "python3 -c 1", "fish line that is not a complete registration"),
        ("fish", "set x $HOME", "expands $HOME"),
        (
            "fish",
            "complete -c libex-core -n '__fish_seen_subcommand_from book; and (id)' -a x",
            "command substitution",
        ),
        (
            "fish",
            "complete -c libex-core -n '__fish_seen_subcommand_from book; and $(id)' -a x",
            "command substitution",
        ),
        (
            "fish",
            "complete -c libex-core -n '__fish_seen_subcommand_from book; and eval x' -a x",
            "fish condition",
        ),
        (
            "fish",
            "complete -c libex-core -n '__fish_seen_subcommand_from book; and test -f /etc/passwd' -a x",
            "fish condition",
        ),
        (
            "fish",
            "complete -c libex-core -n '__fish_seen_subcommand_from book; and not test -f x' -a x",
            "fish condition",
        ),
        (
            "fish",
            "complete -c libex-core -n '__fish_seen_subcommand_from search; and not __fish_seen_subcommand_from abs; and not __fish_seen_subcommand_from x' -l limit",
            "fish condition",
        ),
        (
            "fish",
            "complete -c libex-core -n '__fish_seen_subcommand_from search; and not __fish_seen_subcommand_from abs || id' -l limit",
            "fish condition",
        ),
        (
            "fish",
            "complete -c libex-core -n '__fish_seen_subcommand_from search; and not not __fish_seen_subcommand_from abs' -l limit",
            "fish condition",
        ),
        (
            "fish",
            "complete -c libex-core -n '__fish_seen_subcommand_from book; or __fish_seen_subcommand_from get' -a x",
            "fish condition",
        ),
        (
            "fish",
            "complete -c libex-core -n '__fish_seen_subcommand_from a; and __fish_seen_subcommand_from b; and __fish_seen_subcommand_from c' -a x",
            "fish condition",
        ),
        (
            "fish",
            "complete -c libex-core -n '__fish_seen_subcommand_from book; and __fish_seen_subcommand_from get $x' -a x",
            "fish condition",
        ),
        (
            "fish",
            "complete -c libex-core -n '__fish_seen_subcommand_from book; and __fish_seen_subcommand_from get' -a 'x' > /tmp/x",
            "shell operator",
        ),
    ],
)
def test_the_completion_check_catches_each_planted_form(shell, planted, expected):
    """Each planted line is appended to the real committed script; a check that
    passed it would be passing for a reason other than the script being safe."""
    text = _committed(_COMPLETIONS[shell]) + "\n" + planted + "\n"
    problems = _completion_problems(shell, text)
    assert any(expected in problem for problem in problems), problems


@pytest.mark.parametrize(
    ("shell", "planted", "problem"),
    [
        # Each is otherwise a well-formed registration, so the named check is
        # the only thing that can flag it.
        ("fish", "complete -c libex-core -n __fish_use_subcommand -a x -d 'eval'", "eval"),
        ("fish", "complete -c libex-core -n __fish_use_subcommand -a x -d '$(id)'", "command substitution $("),
        ("fish", "complete -c libex-core -n __fish_use_subcommand -a x -d '`id`'", "backtick"),
    ],
)
def test_each_whole_script_check_flags_its_own_form_on_its_own(shell, planted, problem):
    text = _committed(_COMPLETIONS[shell]) + "\n" + planted + "\n"
    assert problem in _completion_problems(shell, text)
