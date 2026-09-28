"""
Shape checks for the five published Compose stacks that
tests/test_compose_env_parity.py deliberately does not make: that file
proves the three files it reasons about agree on WHICH settings exist: this
one proves the network and VPN-sidecar SHAPE the public-stacks split
introduced is actually wired the way it is documented to be. Kept as a
separate file rather than grown onto test_compose_env_parity.py, which is
already 1000+ lines on its own -- these are a different kind of check
(topology and interpolation shape, not name-set membership) with no
Settings-field angle to share with it.

The five files:

- `docker-compose.yml` -- the API (`libex`), an example VPN sidecar
  (`libex-vpn`), and `postgres`. Creates the `libex-db` and `libex-egress`
  networks, both pinned by name so the other stacks can join them by name.
- `docker-compose.seeder.yml` -- `libex-seeder` and its sidecar
  `libex-seeder-vpn`. Joins `libex-db` as external.
- `docker-compose.backup.yml` -- `libex-backup` only. No VPN: it talks to
  Postgres and to its own backup destination, never to Audible.
- `docker-compose.backfill.yml` -- `chapter-backfill` and its sidecar
  `libex-backfill-vpn`, a one-shot job.
- `docker-compose.refresh.yml` -- `libex-refresh-corpus` and its sidecar
  `libex-refresh-vpn`, a one-shot job.

Four services reach Audible and therefore require their own VPN exit: libex,
libex-seeder, chapter-backfill and libex-refresh-corpus. Each is paired with
its own example gluetun sidecar and its own egress network, and none of the
four shares an egress network with another -- a shared exit would let one
stack's traffic pattern implicate another's. libex-backup is the one
Libex-image service that reaches none of this: it never talks to Audible, so
it carries no proxy variable, no sidecar, and no egress network membership.

These are the properties checked, each a direction below:

1. Every Audible-facing service's own proxy variable is required (`:?`), not
   defaulted -- a blank proxy must refuse to start that container, never
   silently go direct.
2. No `*_WG_*` placeholder is required -- an operator using a proxy that is
   not the bundled gluetun sidecar must never be forced to fill in
   WireGuard keys a sidecar they removed will never read.
3. Every `WIREGUARD_*` value the sidecars pass to gluetun is a pure
   `${...}` reference, carrying no literal text a stray edit could smuggle
   in front of or behind the interpolation.
4. Every sidecar is pinned to the `:v3` release line, publishes no port, and
   sets none of `privileged`, `network_mode` or `depends_on`.
5. Every sidecar sits on its own egress network only -- never on `default`,
   `libex-db`, or another stack's egress network.
6. The four egress networks are declared with a pinned `name:`, matching the
   pattern `libex-db` already set: a network another stack joins by name
   has to have a name that does not depend on the deploying project's
   directory name, which is what Compose derives it from otherwise.
7. Every Audible-facing service is a member of its own egress network.
8. `postgres` and `libex-backup` are never a member of any egress network --
   neither reaches Audible, and an egress membership on either would put
   database or backup traffic behind a VPN exit built to carry Audible
   traffic only.
9. The three job services -- libex-seeder, chapter-backfill,
   libex-refresh-corpus -- are not on `default`. Unlike libex-backup, none
   of the three has a reason to be reachable at all beyond its own egress
   and `libex-db` networks.
10. The seeder, backfill and refresh proxy variables' `:?` error message
    names the stack, so an operator who copies the API's proxy URL into the
    wrong stack's `.env` is told why it is rejected rather than silently
    reusing the API's own exit.
11. Container names are unique across all five files -- Compose would
    otherwise let a second stack collide with a container from the first,
    and a name collision is diagnosed at `docker compose up` time, per
    stack, not by anything that reads the files together the way this test
    does.
12. `docker-compose.backup.yml` declares no gluetun service at all --
    the written record that this stack deliberately has no VPN, checked
    mechanically rather than left to the file's own comment.

Honest limit: like test_compose_env_parity.py, this is a structural check on
the parsed YAML and a text scan for the `${...}` interpolation shape. It has
no opinion on whether the *values themselves* are sane at runtime -- it
would not catch a WireGuard key that is well-formed as a string but wrong,
or a proxy URL that resolves but points at someone else's exit.
"""

# Standard library
import re
from pathlib import Path

# Third party
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_PATH = REPO_ROOT / "docker-compose.yml"
SEEDER_COMPOSE_PATH = REPO_ROOT / "docker-compose.seeder.yml"
BACKUP_COMPOSE_PATH = REPO_ROOT / "docker-compose.backup.yml"
BACKFILL_COMPOSE_PATH = REPO_ROOT / "docker-compose.backfill.yml"
REFRESH_COMPOSE_PATH = REPO_ROOT / "docker-compose.refresh.yml"
ALL_COMPOSE_PATHS = (
    COMPOSE_PATH,
    SEEDER_COMPOSE_PATH,
    BACKUP_COMPOSE_PATH,
    BACKFILL_COMPOSE_PATH,
    REFRESH_COMPOSE_PATH,
)

# Every service that reaches Audible and therefore requires its own VPN
# exit, mapped to the interpolation variable its AUDIBLE_PROXY_URL entry is
# built from and the lowercase token its `:?` message must name (None for
# libex itself, which carries no such token -- only the job stacks need one,
# to stop an operator pointing a job stack at the API's own proxy by habit).
AUDIBLE_FACING_SERVICES: dict[Path, tuple[str, str, str | None]] = {
    COMPOSE_PATH: ("libex", "AUDIBLE_PROXY_URL", None),
    SEEDER_COMPOSE_PATH: ("libex-seeder", "SEEDER_PROXY_URL", "seeder"),
    BACKFILL_COMPOSE_PATH: ("chapter-backfill", "BACKFILL_PROXY_URL", "backfill"),
    REFRESH_COMPOSE_PATH: ("libex-refresh-corpus", "REFRESH_PROXY_URL", "refresh"),
}

# Every example gluetun sidecar, mapped to the pinned egress network it and
# only it sits on.
SIDECAR_SERVICES: dict[Path, tuple[str, str]] = {
    COMPOSE_PATH: ("libex-vpn", "libex-egress"),
    SEEDER_COMPOSE_PATH: ("libex-seeder-vpn", "libex-seeder-egress"),
    BACKFILL_COMPOSE_PATH: ("libex-backfill-vpn", "libex-backfill-egress"),
    REFRESH_COMPOSE_PATH: ("libex-refresh-vpn", "libex-refresh-egress"),
}

ALL_EGRESS_NETWORKS: set[str] = {network for _, network in SIDECAR_SERVICES.values()}

# The three standalone job services -- deliberately excludes libex-backup,
# which keeps `default` in its own file on purpose (it has no VPN and no
# reason to avoid its own project's default network the way a job stack
# reaching out to Audible over a dedicated exit does).
JOB_SERVICES: dict[Path, str] = {
    SEEDER_COMPOSE_PATH: "libex-seeder",
    BACKFILL_COMPOSE_PATH: "chapter-backfill",
    REFRESH_COMPOSE_PATH: "libex-refresh-corpus",
}

WIREGUARD_KEYS = (
    "WIREGUARD_PRIVATE_KEY",
    "WIREGUARD_ADDRESSES",
    "WIREGUARD_ENDPOINT_IP",
    "WIREGUARD_ENDPOINT_PORT",
    "WIREGUARD_PUBLIC_KEY",
    "WIREGUARD_PRESHARED_KEY",
)


def _load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def _load_text(path: Path) -> str:
    return path.read_text()


def _service_environment_values(compose: dict, service: str) -> dict[str, str]:
    """Name -> raw right-hand side (interpolation syntax included) for every
    entry in the given service's `environment:` list."""
    env_list = compose["services"][service]["environment"]
    return dict(item.partition("=")[::2] for item in env_list)


def _service_networks(compose: dict, service: str) -> list[str]:
    return list(compose["services"][service].get("networks") or [])


def test_audible_facing_proxy_urls_are_required():
    """
    Direction 1: every Audible-facing service's AUDIBLE_PROXY_URL entry is
    built from an interpolation using `:?`, never `:-` -- a blank value must
    stop that specific container from starting, not fall back to a default
    that sends Audible traffic out unproxied. This is the compose-side half
    of the same invariant enforced in code: Libex refuses to run with a
    blank proxy unless direct egress was explicitly chosen, and a `:-`
    default here would let a stack silently choose that for an operator who
    never meant to.

    Also proves BACKFILL_PROXY_URL and REFRESH_PROXY_URL are the interpolation
    variables actually wired into their stacks' AUDIBLE_PROXY_URL entries --
    not merely present somewhere in the file, which is all
    test_env_example_names_are_reachable_in_compose (in
    test_compose_env_parity.py) can tell.
    """
    for path, (service, var, _token) in AUDIBLE_FACING_SERVICES.items():
        compose = _load_yaml(path)
        values = _service_environment_values(compose, service)
        proxy_value = values.get("AUDIBLE_PROXY_URL", "")
        assert f"${{{var}:?" in proxy_value, (
            f"{path.name}'s {service} service's AUDIBLE_PROXY_URL entry is "
            f"{proxy_value!r}, which does not require {var} via `:?`. A "
            "blank proxy must refuse to start this container rather than "
            "silently default to something -- change the interpolation back "
            "to `:?`."
        )


def test_wg_placeholders_are_never_required():
    """
    Direction 2: none of the 24 `*_WG_*` interpolation tokens (six each for
    API, SEEDER, BACKFILL, REFRESH) uses `:?`. Every one of these six-tuples
    is read only by that stack's own bundled example gluetun sidecar --
    an operator bringing their own VPN removes the sidecar service entirely
    and leaves all six blank, per .env.example's own instructions. A `:?`
    on any of them would force that operator to fill in WireGuard keys for a
    container they deliberately do not run.
    """
    offenders = []
    for path in ALL_COMPOSE_PATHS:
        text = _load_text(path)
        for match in re.finditer(r"\$\{([A-Za-z_]*_WG_[A-Za-z_]*):\?", text):
            offenders.append(f"{path.name}: {match.group(1)}")

    assert not offenders, (
        f"Found *_WG_* interpolation(s) using `:?` (required) instead of "
        f"`:-` (optional): {offenders}. Every *_WG_* placeholder is read "
        "only by that stack's own bundled example sidecar and must stay "
        "optional, or an operator using their own VPN instead of the "
        "bundled one is forced to fill in keys for a container they never run."
    )


def test_wireguard_values_are_pure_interpolation():
    """
    Direction 3: every `WIREGUARD_*` entry in every sidecar's `environment:`
    block is a bare `${NAME:-default}` reference with nothing else in the
    value -- no literal prefix or suffix a copy-paste edit could leave
    behind. Verified against the exact shape the files ship today
    (`${..._WG_...:-}`, an empty default) rather than merely "contains a
    `${...}` somewhere", so a stray character anywhere in the value fails
    this rather than passing because a `${...}` happens to appear in it.
    """
    for path, (sidecar, _network) in SIDECAR_SERVICES.items():
        compose = _load_yaml(path)
        values = _service_environment_values(compose, sidecar)
        for key in WIREGUARD_KEYS:
            value = values.get(key)
            assert value is not None, (
                f"{path.name}'s {sidecar} service is missing {key} from its "
                "`environment:` block entirely."
            )
            assert re.fullmatch(r"\$\{[A-Za-z_][A-Za-z0-9_]*:-[^}]*\}", value), (
                f"{path.name}'s {sidecar} service sets {key}={value!r}, "
                "which is not a pure `${NAME:-default}` interpolation. Every "
                "WIREGUARD_* value the sidecars pass to gluetun must be "
                "exactly that shape -- nothing else in app/ or scripts/ "
                "reads these, so a value that has drifted from the pattern "
                "is a compose-file typo, not a code change."
            )


def test_sidecars_are_pinned_v3_with_no_extra_privileges():
    """
    Direction 4: every example gluetun sidecar is pinned to the `:v3`
    release line (never `:latest`, which would silently change behaviour
    under an operator who never asked for an upgrade) and sets none of
    `ports`, `privileged`, `network_mode` or `depends_on`. The published
    stacks give the sidecar no route out except the network list itself and
    no reason to run with anything beyond `NET_ADMIN` -- any of those four
    keys reappearing is a live capability the published stacks deliberately
    never grant, not a style preference.
    """
    forbidden_keys = ("ports", "privileged", "network_mode", "depends_on")
    for path, (sidecar, _network) in SIDECAR_SERVICES.items():
        compose = _load_yaml(path)
        definition = compose["services"][sidecar]

        image = definition.get("image", "")
        assert re.search(r":v3(\.\d+)*$", image), (
            f"{path.name}'s {sidecar} service image is {image!r}, not "
            "pinned to the v3 release line. An unpinned or wrong-major "
            "gluetun image can change behaviour under an operator who never "
            "asked for the upgrade."
        )

        present = [key for key in forbidden_keys if key in definition]
        assert not present, (
            f"{path.name}'s {sidecar} service sets {present}, which the "
            "published stacks deliberately omit on every sidecar: no "
            "published port (never expose the proxy), no privileged mode, "
            "no network_mode override, and no depends_on. Remove them."
        )


def test_sidecars_are_only_on_their_own_egress_network():
    """
    Direction 5: every sidecar's `networks:` list is exactly its own egress
    network -- a single-element list, never `default`, `libex-db`, or
    another stack's egress network. A sidecar sharing `libex-db` would put
    Postgres traffic behind a proxy meant to carry Audible traffic only; a
    sidecar sharing another stack's egress network would blur which stack's
    traffic pattern belongs to which exit, which is exactly what one exit
    per stack means to prevent.
    """
    for path, (sidecar, network) in SIDECAR_SERVICES.items():
        compose = _load_yaml(path)
        networks = _service_networks(compose, sidecar)
        assert networks == [network], (
            f"{path.name}'s {sidecar} service is on networks {networks}, "
            f"not exactly [{network!r}]. A sidecar must sit on its own "
            "egress network alone -- never libex-db, never default, never "
            "another stack's egress network."
        )


def test_egress_networks_have_pinned_names():
    """
    Direction 6: each of the four egress networks is declared, in the file
    that creates it, with an explicit pinned `name:` equal to itself and no
    `external: true` -- the same pattern docker-compose.yml already uses for
    libex-db. Compose's default network name is derived from the deploying
    project's directory name, which nothing here controls in a Portainer
    stack; a pinned name is what lets a VPN container in a separate stack
    join it reliably by name, and what makes `docker network rm <name>` the
    documented recovery in .env.example actually correct.
    """
    network_owners = {
        "libex-egress": COMPOSE_PATH,
        "libex-seeder-egress": SEEDER_COMPOSE_PATH,
        "libex-backfill-egress": BACKFILL_COMPOSE_PATH,
        "libex-refresh-egress": REFRESH_COMPOSE_PATH,
    }
    assert set(network_owners) == ALL_EGRESS_NETWORKS, (
        "network_owners in this test and ALL_EGRESS_NETWORKS (derived from "
        "SIDECAR_SERVICES) disagree -- fix this test before trusting its "
        "result."
    )

    for network, path in network_owners.items():
        compose = _load_yaml(path)
        definition = compose["networks"][network]
        assert definition.get("name") == network, (
            f"{path.name}'s {network!r} network does not declare a pinned "
            f"`name:` equal to itself (got {definition.get('name')!r}). "
            "Without it, Compose derives the real Docker network name from "
            "the deploying project's directory, which a separate stack's "
            "VPN sidecar cannot join reliably by name."
        )
        assert not definition.get("external"), (
            f"{path.name}'s {network!r} network is marked external, but "
            f"this is the file that is meant to create it (the sidecar and "
            "the service that uses it both live here). Only libex-db is "
            "ever external, and only in the other four files."
        )


def test_audible_facing_services_join_their_own_egress_network():
    """
    Direction 7: every Audible-facing service is a member of the egress
    network its own sidecar sits on -- the network that is meant to carry
    its Audible traffic out. Missing this would leave the service with no
    route to its exit at all, or -- more dangerously if `default` is still
    present -- no forcing function stopping its Audible requests from taking
    a different route than the one operators are told to expect.
    """
    for path, (service, _var, _token) in AUDIBLE_FACING_SERVICES.items():
        _sidecar, network = SIDECAR_SERVICES[path]
        compose = _load_yaml(path)
        networks = _service_networks(compose, service)
        assert network in networks, (
            f"{path.name}'s {service} service is on networks {networks}, "
            f"which does not include {network!r} -- its own stack's egress "
            "network. Without it, this service has no route to its VPN "
            "sidecar's proxy at all."
        )


def test_postgres_and_backup_never_join_an_egress_network():
    """
    Direction 8: neither `postgres` (docker-compose.yml) nor `libex-backup`
    (docker-compose.backup.yml) is ever a member of any of the four egress
    networks. Neither service reaches Audible; an egress membership on
    either would route database or backup traffic behind a VPN exit built
    and reasoned about as an Audible-only path -- exactly the kind of route
    out the published stacks deliberately never grant to a service that
    never talks to Audible.
    """
    compose = _load_yaml(COMPOSE_PATH)
    postgres_networks = set(_service_networks(compose, "postgres"))
    leaked = postgres_networks & ALL_EGRESS_NETWORKS
    assert not leaked, (
        f"docker-compose.yml's postgres service is on egress network(s) "
        f"{sorted(leaked)}. postgres never reaches Audible and must never "
        "sit on an egress network."
    )

    backup_compose = _load_yaml(BACKUP_COMPOSE_PATH)
    backup_networks = set(_service_networks(backup_compose, "libex-backup"))
    leaked = backup_networks & ALL_EGRESS_NETWORKS
    assert not leaked, (
        f"docker-compose.backup.yml's libex-backup service is on egress "
        f"network(s) {sorted(leaked)}. libex-backup has no VPN and never "
        "reaches Audible -- it must never sit on an egress network."
    )


def test_job_services_are_not_on_default():
    """
    Direction 9: the three standalone job services -- libex-seeder,
    chapter-backfill, libex-refresh-corpus -- are not members of `default`,
    each stack's own implicit network. Each already has exactly the
    networks it needs (its own egress network, and the external `libex-db`)
    and no other service in its file for `default` to usefully connect it
    to. libex-backup is deliberately not included here: unlike the three job
    stacks, docker-compose.backup.yml keeps `default` for libex-backup on
    purpose (see that file's own `networks:` block), so asserting its
    absence here would fail against a file this test must not edit.
    """
    for path, service in JOB_SERVICES.items():
        compose = _load_yaml(path)
        networks = _service_networks(compose, service)
        assert "default" not in networks, (
            f"{path.name}'s {service} service is on the `default` network, "
            f"which it has no use for -- its networks are {networks}. "
            "Remove `default` from its `networks:` list."
        )


def test_seeder_backfill_refresh_proxy_examples_name_their_stack():
    """
    Direction 10: the seeder, backfill and refresh AUDIBLE_PROXY_URL entries'
    `:?` message gives an example URL whose HOSTNAME contains the lowercase
    token for that stack (seeder, backfill, refresh) -- the same token their
    runtime hostname check enforces. Checked here as a documentation fact:
    this test does not run the app's own validator, only confirms the
    example an operator actually sees names the right stack, so a
    copy-pasted example that named the wrong stack's exit would be caught as
    a compose-file mistake rather than only surfacing the first time someone
    reads two stacks' `.env` side by side.

    Deliberately checks the example HOSTNAME specifically, extracted from
    the `e.g. http://...` clause, rather than searching the whole message
    string for the token: the interpolation variable itself
    (SEEDER_PROXY_URL, BACKFILL_PROXY_URL, REFRESH_PROXY_URL) always
    contains the token as a substring of its own name, so a whole-message
    search would pass even if the example host were replaced with something
    that names no stack at all -- caught only by mutating a scratch copy's
    example host and confirming a whole-message version of this assertion
    kept passing regardless.
    """
    example_pattern = re.compile(r"e\.g\.\s+https?://([^:/\s]+)")
    for path, (service, _var, token) in AUDIBLE_FACING_SERVICES.items():
        if token is None:
            continue
        compose = _load_yaml(path)
        values = _service_environment_values(compose, service)
        proxy_value = values.get("AUDIBLE_PROXY_URL", "")
        match = example_pattern.search(proxy_value)
        assert match, (
            f"{path.name}'s {service} service's AUDIBLE_PROXY_URL message "
            f"{proxy_value!r} has no `e.g. http://<host>:<port>` example to "
            "check the hostname of."
        )
        example_host = match.group(1)
        assert token in example_host.lower(), (
            f"{path.name}'s {service} service's AUDIBLE_PROXY_URL example "
            f"host {example_host!r} does not mention {token!r}. An operator "
            "who copies the API's proxy URL into this stack's .env by habit "
            "is meant to be told, in the example itself, which stack this "
            "proxy has to belong to."
        )


def test_container_names_are_unique_across_all_stacks():
    """
    Direction 11: every `container_name` across all five files is unique.
    Compose lets each stack declare its own container names freely, but the
    Docker daemon they all eventually run against is one shared namespace --
    a second stack reusing a name is a collision `docker compose up`
    diagnoses per stack, at deploy time, never something reading all five
    files together the way an operator planning a deployment would.
    """
    names_by_file: dict[str, list[str]] = {}
    for path in ALL_COMPOSE_PATHS:
        compose = _load_yaml(path)
        for service, definition in compose["services"].items():
            name = definition.get("container_name")
            if name:
                names_by_file.setdefault(name, []).append(f"{path.name}:{service}")

    duplicates = {name: owners for name, owners in names_by_file.items() if len(owners) > 1}
    assert not duplicates, (
        f"container_name value(s) are reused across stacks: {duplicates}. "
        "Every container_name must be unique across all five files -- the "
        "Docker daemon they run against is one shared namespace regardless "
        "of which stack declared the name."
    )


def test_backup_stack_has_no_vpn_sidecar():
    """
    Direction 12: docker-compose.backup.yml declares no service running the
    gluetun image. libex-backup talks only to Postgres and to its own
    backup destination, never to Audible, so a VPN sidecar in this file
    would be dead weight at best and a second, unreasoned-about egress path
    at worst. Checked mechanically rather than left to the file's own "No
    VPN" comment, which nothing enforces if a sidecar is ever pasted in
    alongside it.
    """
    compose = _load_yaml(BACKUP_COMPOSE_PATH)
    gluetun_services = [
        name
        for name, definition in compose["services"].items()
        if re.split(r"[:@]", definition.get("image", ""), maxsplit=1)[0] == "qmcgaw/gluetun"
    ]
    assert not gluetun_services, (
        f"docker-compose.backup.yml declares gluetun service(s) "
        f"{gluetun_services}. libex-backup has no VPN by design -- it never "
        "reaches Audible, only Postgres and its own backup destination."
    )
