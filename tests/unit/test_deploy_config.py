"""Deployment contracts for the request path: one trusted edge, real readiness.

Static assertions over the deployment artefacts. They need no container and no
database, and they fail when the trust boundary or the probe semantics regress:
the API trusts its proxy headers only because it publishes no port and the
gateway overwrites them, and "healthy" must mean the API can reach PostgreSQL.

The contracts are read from *live* text: nginx comments are stripped before
parsing (a directive that survives only inside a comment is not a control) and
the last HEALTHCHECK is the one Docker runs. A guard that a commented-out block
satisfies is not a guard.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY = REPO_ROOT / "deploy"
NGINX_CONF = DEPLOY / "nginx.conf"
DOCKERFILE = DEPLOY / "Dockerfile"
COMPOSE = DEPLOY / "docker-compose.prod.yml"

_XFF_DIRECTIVE = re.compile(
    r"^[ \t]*proxy_set_header[ \t]+X-Forwarded-For[ \t]+(?P<value>[^;]+);[ \t]*$",
    re.MULTILINE | re.IGNORECASE,
)
_SERVER_OPEN = re.compile(r"(?m)^[ \t]*server[ \t]*\{")
_LOCATION_OPEN = re.compile(r"(?m)^[ \t]*location[ \t]+(?P<header>[^{]+)\{")
# Keys that would make the API reachable without the gateway, or bypass the
# "no published port" half of the trust argument.
_API_FORBIDDEN_KEYS = ("network_mode", "ports", "publish", "published")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _strip_nginx_comments(config: str) -> str:
    """Return the config without comments, keeping the line structure.

    nginx starts a comment at `#` outside a quoted string. Comments are removed
    rather than skipped so a commented-out server block, location or
    `proxy_set_header` cannot satisfy a contract (T-38 review, mutation A).
    """
    live: list[str] = []
    for line in config.splitlines(keepends=True):
        quote: str | None = None
        cut = len(line)
        for index, character in enumerate(line):
            if quote is not None:
                if character == quote:
                    quote = None
            elif character in "\"'":
                quote = character
            elif character == "#":
                cut = index
                break
        if cut == len(line):
            live.append(line)
        else:
            suffix = "\n" if line.endswith("\n") else ""
            live.append(line[:cut].rstrip() + suffix)
    return "".join(live)


def _nginx_server_blocks() -> list[str]:
    """Return every live server block of deploy/nginx.conf."""
    config = _strip_nginx_comments(_read(NGINX_CONF))
    blocks: list[str] = []
    cursor = 0
    while (match := _SERVER_OPEN.search(config, cursor)) is not None:
        depth = 0
        for end in range(match.end() - 1, len(config)):
            if config[end] == "{":
                depth += 1
            elif config[end] == "}":
                depth -= 1
                if depth == 0:
                    blocks.append(config[match.start() : end + 1])
                    cursor = end + 1
                    break
        else:
            pytest.fail("unbalanced server block in deploy/nginx.conf")
    assert blocks, "deploy/nginx.conf declares no live server block"
    return blocks


def _nginx_server_block(listen: str) -> str:
    """Return the single live server block whose listen directive matches."""
    pattern = re.compile(rf"(?m)^[ \t]*listen[ \t]+{listen}[ \t]*;")
    matches = [block for block in _nginx_server_blocks() if pattern.search(block)]
    assert len(matches) == 1, (
        f"deploy/nginx.conf must have exactly one live server block with listen {listen!r};"
        f" found {len(matches)}"
    )
    return matches[0]


def _nginx_locations(server: str) -> dict[str, str]:
    """Return every live location block of one server block, keyed by its header."""
    locations: dict[str, str] = {}
    cursor = 0
    while (match := _LOCATION_OPEN.search(server, cursor)) is not None:
        header = "location " + match.group("header").strip()
        depth = 0
        for end in range(match.end() - 1, len(server)):
            if server[end] == "{":
                depth += 1
            elif server[end] == "}":
                depth -= 1
                if depth == 0:
                    locations[header] = server[match.start() : end + 1]
                    cursor = end + 1
                    break
        else:
            pytest.fail("unbalanced location block in deploy/nginx.conf")
    return locations


def _assert_forwarded_for_is_overwritten(location: str, label: str) -> None:
    """One X-Forwarded-For directive per proxy site, and it must name the peer.

    nginx sends duplicate `proxy_set_header` headers in directive order and
    uvicorn (with `--forwarded-allow-ips "*"`) trusts the first entry, so a second
    directive carrying the client's header would let the client choose its own
    address (T-38 review, mutation C).
    """
    values = [match.group("value").strip() for match in _XFF_DIRECTIVE.finditer(location)]
    assert len(values) == 1, (
        f"{label} must set exactly one X-Forwarded-For header; found {len(values)}: {values}"
    )
    assert values[0] == "$remote_addr", (
        f"{label} must overwrite X-Forwarded-For with $remote_addr, not {values[0]!r}"
    )


def _compose_service(name: str) -> str:
    """Return the lines of one compose service, up to the next top-level key."""
    lines = _read(COMPOSE).splitlines()
    header = f"  {name}:"
    if header not in lines:
        pytest.fail(f"deploy/docker-compose.prod.yml declares no {name!r} service")
    start = lines.index(header)
    end = len(lines)
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if line.strip() and not line.startswith("    "):
            end = index
            break
    return "\n".join(lines[start:end])


def _assert_api_cannot_be_reached_without_the_gateway(api: str) -> None:
    for key in _API_FORBIDDEN_KEYS:
        match = re.search(rf"(?m)^[ \t]+{key}[ \t]*:", api)
        assert match is None, (
            f"the api service must not declare {key!r}: that would expose the API"
            " without the gateway and void the forwarded-header trust"
        )


def _dockerfile_healthchecks() -> list[str]:
    """Return every HEALTHCHECK instruction, including its line continuations."""
    lines = _read(DOCKERFILE).splitlines()
    starts = [
        index
        for index, line in enumerate(lines)
        if re.match(r"^\s*HEALTHCHECK\b", line, re.IGNORECASE)
    ]
    if not starts:
        pytest.fail("deploy/Dockerfile declares no HEALTHCHECK")
    instructions: list[str] = []
    for start in starts:
        end = start
        while lines[end].rstrip().endswith("\\"):
            end += 1
        instructions.append("\n".join(lines[start : end + 1]))
    return instructions


def _dockerfile_healthcheck() -> str:
    """Return the HEALTHCHECK Docker runs: the last one declared."""
    return _dockerfile_healthchecks()[-1]


def test_gateway_overwrites_the_forwarded_chain_with_the_peer() -> None:
    """The edge replaces any client-supplied chain; it never appends to it."""
    raw = _read(NGINX_CONF)
    assert "proxy_add_x_forwarded_for" not in raw
    assert "$http_x_forwarded_for" not in raw

    tls = _nginx_server_block("443 ssl")
    _assert_forwarded_for_is_overwritten(tls, "the TLS server block")


def test_every_proxy_site_overwrites_the_forwarded_chain() -> None:
    """All three proxy sites set the peer address, not a client-supplied chain."""
    sites = 0
    for listen in ("80", "443 ssl"):
        server = _nginx_server_block(listen)
        for header, block in _nginx_locations(server).items():
            if "proxy_pass" not in block:
                continue
            sites += 1
            _assert_forwarded_for_is_overwritten(block, f"{header!r} in the listen {listen} server")
    assert sites == 3, (
        "expected three proxy sites (two plain-HTTP health probes and the TLS catch-all),"
        f" found {sites}"
    )


def test_gateway_sets_the_peer_address_and_the_tls_proto() -> None:
    tls = _nginx_server_block("443 ssl")
    assert "proxy_set_header X-Real-IP $remote_addr;" in tls
    assert "proxy_set_header X-Forwarded-Proto https;" in tls


def test_image_healthcheck_probes_readiness() -> None:
    """A container must not report healthy while its database is unavailable."""
    healthcheck = _dockerfile_healthcheck()
    assert "http://127.0.0.1:8000/api/v1/health/ready" in healthcheck
    assert "http://127.0.0.1:8000/api/v1/health'" not in healthcheck
    assert "--start-period=30s" in healthcheck


def test_compose_api_healthcheck_probes_readiness() -> None:
    api = _compose_service("api")
    assert "http://127.0.0.1:8000/api/v1/health/ready" in api
    assert "http://127.0.0.1:8000/api/v1/health'" not in api


def test_api_service_publishes_no_host_port() -> None:
    """The gateway is the only way in; that is half of why its headers are trusted."""
    assert "8000:8000" not in _read(COMPOSE)

    api = _compose_service("api")
    assert "expose:" in api
    assert '- "8000"' in api
    _assert_api_cannot_be_reached_without_the_gateway(api)


def test_plain_http_health_locations_are_live() -> None:
    """The forwarding must be live text: a commented-out block is not a control."""
    locations = _nginx_locations(_nginx_server_block("80"))
    for header in ("location = /api/v1/health", "location = /api/v1/health/ready"):
        assert header in locations, f"the plain-HTTP server must have a live {header!r} block"


def test_plain_http_forwards_the_health_probes() -> None:
    """http://.../api/v1/health must answer 200, not a redirect that drops the port."""
    locations = _nginx_locations(_nginx_server_block("80"))
    for header in ("location = /api/v1/health", "location = /api/v1/health/ready"):
        assert header in locations, f"the plain-HTTP server must forward {header}"
        forward = locations[header]
        assert "proxy_pass http://api:8000;" in forward
        assert "proxy_set_header X-Real-IP $remote_addr;" in forward
        assert "proxy_set_header X-Forwarded-For $remote_addr;" in forward
        assert "proxy_set_header X-Forwarded-Proto http;" in forward
        _assert_forwarded_for_is_overwritten(forward, header)


def test_plain_http_still_redirects_every_other_path() -> None:
    """Only the probes are forwarded; the rest still climbs to TLS on the bare host."""
    locations = _nginx_locations(_nginx_server_block("80"))
    assert "location /" in locations
    assert "return 301 https://$host$request_uri;" in locations["location /"]
    probes = [
        block
        for header, block in locations.items()
        if header.startswith("location = /api/v1/health")
    ]
    assert len(probes) == 2
    assert all("return 301" not in block for block in probes)


def test_gateway_healthcheck_probes_readiness() -> None:
    """The gateway is healthy only when the API behind it can serve."""
    gateway = _compose_service("gateway")
    assert "https://127.0.0.1/api/v1/health/ready" in gateway
    assert "https://127.0.0.1/api/v1/health || exit 1" not in gateway
