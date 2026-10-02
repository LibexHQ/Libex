# fish completion for libex-core, generated from the command definitions, do not edit by hand
complete -c libex-core -f
complete -c libex-core -n __fish_use_subcommand -s h -l help -d 'show this help message and exit'
complete -c libex-core -n __fish_use_subcommand -l version -d 'show the version number and exit'
complete -c libex-core -n __fish_use_subcommand -s q -l quiet -d 'print only the final error line'
complete -c libex-core -n __fish_use_subcommand -s v -l verbose -d 'more detail on standard error, repeat as -vv for debug output with tracebacks'
complete -c libex-core -n __fish_use_subcommand -a completion -d 'print a shell completion script'
complete -c libex-core -n __fish_use_subcommand -a config -d 'show how requests would leave this machine'
complete -c libex-core -n '__fish_seen_subcommand_from completion' -s h -l help -d 'show this help message and exit'
complete -c libex-core -n '__fish_seen_subcommand_from completion' -a 'bash zsh fish'
complete -c libex-core -n '__fish_seen_subcommand_from config' -s h -l help -d 'show this help message and exit'
