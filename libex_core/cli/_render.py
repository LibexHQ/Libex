"""
The man page and the shell completion scripts, rendered from the parser.

Output is a pure function of the parser's structure and of strings written in
this package: no date, no version, no terminal width, no locale. argparse's
help formatter is deliberately not used, because it reads COLUMNS and its
wording differs between Python versions, so a committed copy could never be
compared against a fresh render. The completion scripts hold literal words
only; nothing in them runs a command, reads an environment variable of its
own, or calls back into this program.

The committed copies live under libex-core-data/ and are written by

    python -m libex_core.cli._render --write libex-core-data
"""

import argparse
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from libex_core.cli.environment import VARIABLES
from libex_core.cli.exit_codes import DESCRIPTIONS, ExitCode
from libex_core.cli.parser import PROG, build_parser

# Paths under the data directory, which is where they install under the
# environment prefix.
MAN_PATH = "share/man/man1/libex-core.1"
BASH_PATH = "share/bash-completion/completions/libex-core"
ZSH_PATH = "share/zsh/site-functions/_libex-core"
FISH_PATH = "share/fish/vendor_completions.d/libex-core.fish"

_SCRIPT_PATHS = {"bash": BASH_PATH, "zsh": ZSH_PATH, "fish": FISH_PATH}

# Words a completion script may contain, and the looser set descriptions may
# use. Neither allows a quote, a backslash, a dollar sign, a backtick, a
# colon or a bracket, so no value needs quoting per shell, and one that would
# is refused instead of being escaped.
_WORD = re.compile(r"[A-Za-z0-9_.-]+")
_DESCRIPTION = re.compile(r"[A-Za-z0-9 ,.()/_-]+")

_GENERATED = "generated from the command definitions, do not edit by hand"


@dataclass(frozen=True)
class _Option:
    flags: tuple[str, ...]
    help: str
    exits: bool


@dataclass(frozen=True)
class _Positional:
    name: str
    help: str
    choices: tuple[str, ...]


@dataclass(frozen=True)
class _Command:
    name: str
    summary: str
    description: str
    options: tuple[_Option, ...]
    positionals: tuple[_Positional, ...]


@dataclass(frozen=True)
class _Spec:
    description: str
    options: tuple[_Option, ...]
    # What the completion scripts offer: the top-level words. A command that
    # only holds further commands lists their names as its one positional.
    commands: tuple[_Command, ...]
    # What the man page documents: every command that does something, a
    # nested one under its full name.
    documented: tuple[_Command, ...]


def _read_parser(parser: argparse.ArgumentParser) -> tuple[
    tuple[_Option, ...], tuple[_Positional, ...], Mapping[str, argparse.ArgumentParser], dict[str, str]
]:
    options: list[_Option] = []
    positionals: list[_Positional] = []
    subparsers: Mapping[str, argparse.ArgumentParser] = {}
    summaries: dict[str, str] = {}
    for action in parser._actions:
        if isinstance(action.choices, Mapping):
            subparsers = action.choices
            summaries = {sub.dest: sub.help or "" for sub in action._get_subactions()}
        elif action.option_strings:
            options.append(
                _Option(
                    flags=tuple(action.option_strings),
                    help=action.help or "",
                    # help and version stop parsing; they are the only
                    # actions whose default is argparse.SUPPRESS.
                    exits=action.default is argparse.SUPPRESS,
                )
            )
        else:
            positionals.append(
                _Positional(
                    name=action.metavar or action.dest,
                    help=action.help or "",
                    choices=tuple(action.choices or ()),
                )
            )
    return tuple(options), tuple(positionals), subparsers, summaries


def _read_command(name: str, summary: str, sub: argparse.ArgumentParser) -> _Command:
    options, positionals, _, _ = _read_parser(sub)
    return _Command(
        name=name,
        summary=summary,
        description=sub.description or "",
        options=options,
        positionals=positionals,
    )


def _read_spec(parser: argparse.ArgumentParser) -> _Spec:
    options, _, subparsers, summaries = _read_parser(parser)
    commands = []
    documented = []
    for name, sub in subparsers.items():
        _, _, nested, nested_summaries = _read_parser(sub)
        if not nested:
            command = _read_command(name, summaries.get(name, ""), sub)
            commands.append(command)
            documented.append(command)
            continue
        commands.append(
            _Command(
                name=name,
                summary=summaries.get(name, ""),
                description=sub.description or "",
                options=(),
                positionals=(
                    _Positional(
                        name="command",
                        help="the command to run",
                        choices=tuple(nested),
                    ),
                ),
            )
        )
        for nested_name, leaf in nested.items():
            documented.append(
                _read_command(
                    f"{name} {nested_name}", nested_summaries.get(nested_name, ""), leaf
                )
            )
    return _Spec(
        parser.description or "", options, tuple(commands), tuple(documented)
    )


def _word(value: str) -> str:
    if not _WORD.fullmatch(value):
        raise ValueError(f"not safe to emit into a completion script: {value!r}")
    return value


def _description(value: str) -> str:
    if not _DESCRIPTION.fullmatch(value):
        raise ValueError(f"description not safe to emit into a completion script: {value!r}")
    return value


# ============================================================
# MAN PAGE
# ============================================================

_ROFF_REQUESTS = {"TH", "SH", "SS", "TP", "PP", "B", "RS", "RE"}


def _roff(text: str) -> str:
    text = text.replace("\\", "\\e").replace("-", "\\-")
    return re.sub(r"^([.'])", r"\\&\1", text, flags=re.MULTILINE)


def _roff_flags(flags: tuple[str, ...]) -> str:
    return ", ".join(f"\\fB{_roff(flag)}\\fR" for flag in flags)


def _man_option(option: _Option) -> list[str]:
    return [".TP", _roff_flags(option.flags), _roff(option.help)]


def _man_command(command: _Command) -> list[str]:
    usage = " ".join(
        [f"\\fB{_roff(PROG)} {command.name}\\fR"]
        + [f"\\fI{_roff(p.name)}\\fR" for p in command.positionals]
    )
    lines = [f".SS {command.name}", usage, ".PP", _roff(command.description)]
    for positional in command.positionals:
        text = positional.help
        if positional.choices:
            text += ". One of " + ", ".join(positional.choices) + "."
        lines += [".TP", f"\\fI{_roff(positional.name)}\\fR", _roff(text)]
    for option in command.options:
        lines += _man_option(option)
    return lines


def man_page(parser: argparse.ArgumentParser) -> str:
    spec = _read_spec(parser)
    first_sentence = spec.description.split(". ")[0].rstrip(".")
    name_summary = first_sentence[:1].lower() + first_sentence[1:]
    lines = [
        f'.TH LIBEX\\-CORE 1 "" "{PROG}" "User Commands"',
        ".SH NAME",
        f"{_roff(PROG)} \\- {_roff(name_summary)}",
        ".SH SYNOPSIS",
        f".B {_roff(PROG)}",
        "[\\fIOPTION\\fR]... \\fICOMMAND\\fR [\\fIARGUMENT\\fR]...",
        ".SH DESCRIPTION",
        _roff(spec.description),
        ".SH OPTIONS",
        "Options precede the command.",
    ]
    for option in spec.options:
        lines += _man_option(option)
    lines.append(".SH COMMANDS")
    for command in spec.documented:
        lines += _man_command(command)
    lines.append(".SH ENVIRONMENT")
    for name, text in VARIABLES:
        lines += [".TP", f"\\fB{_roff(name)}\\fR", _roff(text)]
    lines.append(".SH EXIT STATUS")
    for code in ExitCode:
        lines += [".TP", f"\\fB{int(code)}\\fR", _roff(DESCRIPTIONS[code])]
    for line in lines:
        if line.startswith((".", "'")) and line[1:].split(" ")[0] not in _ROFF_REQUESTS:
            raise ValueError(f"unexpected roff request: {line!r}")
    return "\n".join(lines) + "\n"


# ============================================================
# COMPLETION SCRIPTS
# ============================================================

def _flag_words(options: tuple[_Option, ...]) -> list[str]:
    return [_word(flag) for option in options for flag in option.flags]


def _bash(spec: _Spec) -> str:
    top = _flag_words(spec.options) + [_word(c.name) for c in spec.commands]
    branches = []
    for command in spec.commands:
        flags = _flag_words(command.options)
        choices = [_word(c) for p in command.positionals[:1] for c in p.choices]
        if choices:
            branches.append(
                f"        {_word(command.name)})\n"
                f"            if (( COMP_CWORD == ci + 1 )); then\n"
                f"                candidates=({' '.join(flags + choices)})\n"
                f"            else\n"
                f"                candidates=({' '.join(flags)})\n"
                f"            fi\n"
                f"            ;;"
            )
        else:
            branches.append(
                f"        {_word(command.name)}) candidates=({' '.join(flags)}) ;;"
            )
    return (
        f"# bash completion for {PROG}, {_GENERATED}\n"
        "_libex_core() {\n"
        "    local cur cmd ci i word\n"
        "    local -a candidates\n"
        '    cur="${COMP_WORDS[COMP_CWORD]}"\n'
        '    cmd=""\n'
        "    ci=0\n"
        "    for ((i = 1; i < COMP_CWORD; i++)); do\n"
        '        case "${COMP_WORDS[i]}" in\n'
        "            -*) ;;\n"
        '            *) cmd="${COMP_WORDS[i]}"; ci=$i; break ;;\n'
        "        esac\n"
        "    done\n"
        '    case "$cmd" in\n'
        f'        "") candidates=({" ".join(top)}) ;;\n'
        + "\n".join(branches)
        + "\n"
        "        *) candidates=() ;;\n"
        "    esac\n"
        "    COMPREPLY=()\n"
        '    for word in "${candidates[@]}"; do\n'
        '        if [[ $word == "$cur"* ]]; then\n'
        '            COMPREPLY+=("$word")\n'
        "        fi\n"
        "    done\n"
        "}\n"
        f"complete -F _libex_core {PROG}\n"
    )


def _zsh_option(option: _Option) -> str:
    text = _description(option.help)
    flags = [_word(flag) for flag in option.flags]
    prefix = "(- *)" if option.exits else ""
    if len(flags) == 1:
        return f"'{prefix}{flags[0]}[{text}]'"
    leading = f"'{prefix}'" if prefix else ""
    return f"{leading}{{{','.join(flags)}}}'[{text}]'"


def _zsh(spec: _Spec) -> str:
    top_options = "".join(f"        {_zsh_option(o)} \\\n" for o in spec.options)
    entries = "".join(
        f"                '{_word(c.name)}:{_description(c.summary)}'\n"
        for c in spec.commands
    )
    cases = []
    for command in spec.commands:
        specs = [_zsh_option(o) for o in command.options]
        for index, positional in enumerate(command.positionals, start=1):
            choices = " ".join(_word(c) for c in positional.choices)
            specs.append(
                f"'{index}:{_word(positional.name)}:({choices})'"
                if choices
                else f"'{index}:{_word(positional.name)}:'"
            )
        body = " \\\n                        ".join(specs)
        cases.append(
            f"                {_word(command.name)})\n"
            f"                    _arguments \\\n"
            f"                        {body}\n"
            f"                    ;;"
        )
    return (
        f"#compdef {PROG}\n"
        f"# zsh completion for {PROG}, {_GENERATED}\n"
        f"\n"
        f"_{PROG}() {{\n"
        '    local curcontext="$curcontext" state line\n'
        "    typeset -A opt_args\n"
        "\n"
        "    _arguments -C \\\n"
        f"{top_options}"
        "        '1: :->command' \\\n"
        "        '*:: :->args'\n"
        "\n"
        "    case $state in\n"
        "        command)\n"
        "            local -a commands\n"
        "            commands=(\n"
        f"{entries}"
        "            )\n"
        f"            _describe -t commands '{PROG} command' commands\n"
        "            ;;\n"
        "        args)\n"
        "            case ${line[1]} in\n"
        + "\n".join(cases)
        + "\n"
        "            esac\n"
        "            ;;\n"
        "    esac\n"
        "}\n"
        "\n"
        f'_{PROG} "$@"\n'
    )


def _fish_flags(option: _Option) -> str:
    parts = []
    for flag in option.flags:
        word = _word(flag)
        parts.append(f"-l {word[2:]}" if word.startswith("--") else f"-s {word[1:]}")
    return " ".join(parts)


def _fish(spec: _Spec) -> str:
    lines = [
        f"# fish completion for {PROG}, {_GENERATED}",
        f"complete -c {PROG} -f",
    ]
    for option in spec.options:
        lines.append(
            f"complete -c {PROG} -n __fish_use_subcommand "
            f"{_fish_flags(option)} -d '{_description(option.help)}'"
        )
    for command in spec.commands:
        lines.append(
            f"complete -c {PROG} -n __fish_use_subcommand "
            f"-a {_word(command.name)} -d '{_description(command.summary)}'"
        )
    for command in spec.commands:
        condition = f"'__fish_seen_subcommand_from {_word(command.name)}'"
        for option in command.options:
            lines.append(
                f"complete -c {PROG} -n {condition} "
                f"{_fish_flags(option)} -d '{_description(option.help)}'"
            )
        for positional in command.positionals:
            if positional.choices:
                choices = " ".join(_word(c) for c in positional.choices)
                lines.append(f"complete -c {PROG} -n {condition} -a '{choices}'")
    return "\n".join(lines) + "\n"


_RENDERERS = {"bash": _bash, "zsh": _zsh, "fish": _fish}


def completion_script(parser: argparse.ArgumentParser, shell: str) -> str:
    """The shell name only selects one of three fixed renderers; it is never
    used to build a path or a command."""
    renderer = _RENDERERS.get(shell)
    if renderer is None:
        raise ValueError(f"unsupported shell: {shell!r}")
    return renderer(_read_spec(parser))


def artefacts(parser: argparse.ArgumentParser) -> dict[str, str]:
    """Every committed file, keyed by its path under the data directory."""
    files = {MAN_PATH: man_page(parser)}
    for shell, path in _SCRIPT_PATHS.items():
        files[path] = completion_script(parser, shell)
    return files


def write_artefacts(directory: Path) -> None:
    for relative, text in artefacts(build_parser()).items():
        target = directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode("utf-8"))


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] != "--write":
        print("usage: python -m libex_core.cli._render --write DIRECTORY", file=sys.stderr)
        return 2
    write_artefacts(Path(argv[1]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
