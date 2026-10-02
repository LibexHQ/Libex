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
from dataclasses import dataclass, replace
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
# colon, a bracket or a parenthesis, so no value needs quoting per shell and
# nothing a shell could evaluate is emitted. A description's parentheses are
# rewritten to a dash by _description; anything else outside the set is
# refused instead of being escaped.
_WORD = re.compile(r"[A-Za-z0-9_.-]+")
_DESCRIPTION = re.compile(r"[A-Za-z0-9 ,./_-]+")

_GENERATED = "generated from the command definitions, do not edit by hand"


@dataclass(frozen=True)
class _Option:
    flags: tuple[str, ...]
    help: str
    exits: bool
    choices: tuple[str, ...]


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
    # The commands a group holds, each read the same way, to any depth. A
    # command with children has no options or positionals of its own beyond
    # the word that picks a child.
    children: tuple["_Command", ...] = ()


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
                    choices=tuple(action.choices or ()),
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
    options, positionals, nested, nested_summaries = _read_parser(sub)
    children = tuple(
        _read_command(child_name, nested_summaries.get(child_name, ""), child)
        for child_name, child in nested.items()
    )
    if children:
        options = ()
        positionals = (
            _Positional(
                name="command",
                help="the command to run",
                choices=tuple(child.name for child in children),
            ),
        )
    return _Command(
        name=name,
        summary=summary,
        description=sub.description or "",
        options=options,
        positionals=positionals,
        children=children,
    )


def _documented(command: _Command, prefix: str = "") -> list[_Command]:
    """Every command that does something, under its full name; a group is
    only the way to reach the commands it holds."""
    full = f"{prefix}{command.name}"
    if not command.children:
        return [replace(command, name=full)]
    found: list[_Command] = []
    for child in command.children:
        found += _documented(child, f"{full} ")
    return found


def _read_spec(parser: argparse.ArgumentParser) -> _Spec:
    options, _, subparsers, summaries = _read_parser(parser)
    commands = tuple(
        _read_command(name, summaries.get(name, ""), sub)
        for name, sub in subparsers.items()
    )
    documented: list[_Command] = []
    for command in commands:
        documented += _documented(command)
    return _Spec(parser.description or "", options, commands, tuple(documented))


def _word(value: str) -> str:
    if not _WORD.fullmatch(value):
        raise ValueError(f"not safe to emit into a completion script: {value!r}")
    return value


def _description(value: str) -> str:
    value = re.sub(r"\s*\(([^()]*)\)", r" - \1", value).strip()
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
        [f"\\fB{_roff(f'{PROG} {command.name}')}\\fR"]
        + [f"\\fI{_roff(p.name)}\\fR" for p in command.positionals]
    )
    lines = [f".SS {_roff(command.name)}", usage, ".PP", _roff(command.description)]
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
        f'.TH LIBEX\\-CORE 1 "" "{_roff(PROG)}" "User Commands"',
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


def _words(values: tuple[str, ...]) -> str:
    return " ".join(_word(value) for value in values)


def _first_choices(command: _Command) -> tuple[str, ...]:
    return command.positionals[0].choices if command.positionals else ()


# ------------------------------------------------------------
# bash
# ------------------------------------------------------------

def _bash_assign(command: _Command, words: list[str], indent: str) -> list[str]:
    """Set candidates; a flag that takes one of a fixed set of values offers
    that set when it is the word just typed."""
    valued = [o for o in command.options if o.choices]
    if not valued:
        return [f"{indent}candidates=({' '.join(words)})"]
    lines = [f'{indent}case "${{COMP_WORDS[COMP_CWORD - 1]}}" in']
    for option in valued:
        flags = "|".join(_word(flag) for flag in option.flags)
        lines.append(f"{indent}    {flags}) candidates=({_words(option.choices)}) ;;")
    lines.append(f"{indent}    *) candidates=({' '.join(words)}) ;;")
    lines.append(f"{indent}esac")
    return lines


def _bash_body(command: _Command, depth: int, indent: str) -> list[str]:
    """Candidates for a command whose own name is the word at ci + depth - 1."""
    if command.children:
        names = _words(tuple(child.name for child in command.children))
        lines = [
            f"{indent}if (( COMP_CWORD == ci + {depth} )); then",
            f"{indent}    candidates=({names})",
            f"{indent}else",
            f'{indent}    case "${{COMP_WORDS[ci + {depth}]}}" in',
        ]
        for child in command.children:
            lines += _bash_branch(child, depth + 1, f"{indent}        ")
        lines += [
            f"{indent}        *) candidates=() ;;",
            f"{indent}    esac",
            f"{indent}fi",
        ]
        return lines
    flags = _flag_words(command.options)
    choices = [_word(c) for c in _first_choices(command)]
    if not choices:
        return _bash_assign(command, flags, indent)
    return [
        f"{indent}if (( COMP_CWORD == ci + {depth} )); then",
        *_bash_assign(command, flags + choices, f"{indent}    "),
        f"{indent}else",
        *_bash_assign(command, flags, f"{indent}    "),
        f"{indent}fi",
    ]


def _bash_branch(command: _Command, depth: int, indent: str) -> list[str]:
    body = _bash_body(command, depth, indent + "    ")
    if len(body) == 1:
        return [f"{indent}{_word(command.name)}) {body[0].strip()} ;;"]
    return [f"{indent}{_word(command.name)})", *body, f"{indent}    ;;"]


def _bash(spec: _Spec) -> str:
    top = _flag_words(spec.options) + [_word(c.name) for c in spec.commands]
    branches: list[str] = []
    for command in spec.commands:
        branches += _bash_branch(command, 1, "        ")
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


# ------------------------------------------------------------
# zsh
# ------------------------------------------------------------

def _zsh_option(option: _Option) -> str:
    text = _description(option.help)
    flags = [_word(flag) for flag in option.flags]
    prefix = "(- *)" if option.exits else ""
    value = (
        f":{_word(flags[-1].lstrip('-'))}:({_words(option.choices)})"
        if option.choices
        else ""
    )
    if len(flags) == 1:
        return f"'{prefix}{flags[0]}[{text}]{value}'"
    leading = f"'{prefix}'" if prefix else ""
    return f"{leading}{{{','.join(flags)}}}'[{text}]{value}'"


def _zsh_arguments(
    command: _Command, indent: str, lead: tuple[str, ...] = ()
) -> list[str]:
    """_arguments counts positions from the word after the first, which under a
    group is the command's own name, so the words that led here are described
    as positions of their own and the command's positionals follow them."""
    specs = [_zsh_option(o) for o in command.options]
    for index, word in enumerate(lead, start=1):
        specs.append(f"'{index}:command:({_word(word)})'")
    for index, positional in enumerate(command.positionals, start=len(lead) + 1):
        choices = _words(positional.choices)
        specs.append(
            f"'{index}:{_word(positional.name)}:({choices})'"
            if choices
            else f"'{index}:{_word(positional.name)}:'"
        )
    lines = [f"{indent}_arguments \\"]
    lines += [f"{indent}    {spec} \\" for spec in specs[:-1]]
    lines.append(f"{indent}    {specs[-1]}")
    return lines


def _zsh_branch(
    command: _Command, depth: int, indent: str, lead: tuple[str, ...] = ()
) -> list[str]:
    """A case arm for a command whose name is line[depth]; lead holds the
    names of the commands below the top-level one down to and including this
    command's own name, so it is empty for a top-level command."""
    lines = [f"{indent}{_word(command.name)})"]
    inner = f"{indent}    "
    if command.children:
        lines.append(f"{inner}case ${{line[{depth + 1}]}} in")
        for child in command.children:
            lines += _zsh_branch(
                child, depth + 1, f"{inner}    ", (*lead, child.name)
            )
        lines.append(f"{inner}    *)")
        lines += _zsh_arguments(command, f"{inner}        ")
        lines.append(f"{inner}        ;;")
        lines.append(f"{inner}esac")
    else:
        lines += _zsh_arguments(command, inner, lead)
    lines.append(f"{inner};;")
    return lines


def _zsh(spec: _Spec) -> str:
    top_options = "".join(f"        {_zsh_option(o)} \\\n" for o in spec.options)
    entries = "".join(
        f"                '{_word(c.name)}:{_description(c.summary)}'\n"
        for c in spec.commands
    )
    cases: list[str] = []
    for command in spec.commands:
        cases += _zsh_branch(command, 1, "                ")
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


# ------------------------------------------------------------
# fish
# ------------------------------------------------------------

def _fish_flags(option: _Option) -> str:
    parts = []
    for flag in option.flags:
        word = _word(flag)
        parts.append(f"-l {word[2:]}" if word.startswith("--") else f"-s {word[1:]}")
    if option.choices:
        parts.append(f"-r -a '{_words(option.choices)}'")
    return " ".join(parts)


def _descendant_names(command: _Command) -> set[str]:
    names: set[str] = set()
    for child in command.children:
        names.add(child.name)
        names |= _descendant_names(child)
    return names


def _fish_lines(
    command: _Command, path: tuple[str, ...], groups: tuple[str, ...] = ()
) -> list[str]:
    """Completions for a command reached by path, which includes its own name.

    __fish_seen_subcommand_from matches a word anywhere on the line, so a
    top-level command that shares a name with a command inside another group
    would also fire after that group's word; groups holds the group words, if
    any, that hold a command of the same name and turn this command's rows
    off. A group folds them into the one negated test its child-listing row
    already carries, so no row ever needs a third term."""
    if len(path) > 2:
        raise ValueError(
            f"command nested deeper than the completion guard grammar allows: {' '.join(path)}"
        )
    head = "; and ".join(
        f"__fish_seen_subcommand_from {_word(word)}" for word in path
    )
    seen = head
    if groups:
        seen += f"; and not __fish_seen_subcommand_from {_words(groups)}"
    lines = []
    if command.children:
        names = _words(tuple(child.name for child in command.children))
        held = f"{names} {_words(groups)}" if groups else names
        lines.append(
            f"complete -c {PROG} -n '{head}; and not __fish_seen_subcommand_from {held}' "
            f"-a '{names}'"
        )
    for option in command.options:
        lines.append(
            f"complete -c {PROG} -n '{seen}' "
            f"{_fish_flags(option)} -d '{_description(option.help)}'"
        )
    if not command.children:
        for positional in command.positionals:
            if positional.choices:
                lines.append(
                    f"complete -c {PROG} -n '{seen}' -a '{_words(positional.choices)}'"
                )
    for child in command.children:
        lines += _fish_lines(child, (*path, child.name))
    return lines


def _fish_excluded(command: _Command, spec: _Spec) -> tuple[str, ...]:
    """The other top-level groups holding a command named like this one."""
    return tuple(
        group.name
        for group in spec.commands
        if group.name != command.name and command.name in _descendant_names(group)
    )


def _fish_check_unambiguous(spec: _Spec) -> None:
    """A command inside a group is tested as the group word and its own word,
    both anywhere on the line, so two groups that each hold the other's name
    cannot be told apart by any two-term test; refuse them rather than emit
    rows that fire on the wrong line."""
    held = {group.name: {c.name for c in group.children} for group in spec.commands}
    for group, names in held.items():
        for name in names:
            if group in held.get(name, ()):
                raise ValueError(
                    f"commands {group!r} and {name!r} each hold the other: "
                    "the completion guard grammar cannot tell them apart"
                )


def _fish(spec: _Spec) -> str:
    _fish_check_unambiguous(spec)
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
        lines += _fish_lines(command, (command.name,), _fish_excluded(command, spec))
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
