"""Regras puras do repo por projeto: link do GitHub, pin da branch e `.factory.toml`."""

import re
import tomllib
from collections.abc import Iterable
from urllib.parse import urlsplit

FACTORY_CONFIG_PATH = ".factory.toml"

GITHUB_HOSTS = {"github.com", "www.github.com"}
RESERVED_OWNERS = {
    "orgs",
    "apps",
    "settings",
    "marketplace",
    "features",
    "topics",
    "sponsors",
    "enterprises",
    "users",
    "login",
}
OWNER_PATTERN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")
REPO_PATTERN = re.compile(r"[A-Za-z0-9_.-]{1,100}")


class FactoryConfigError(ValueError):
    """`.factory.toml` ausente, inválido ou sem `test_command`."""


def _github_segments(url: str) -> tuple[str, str, list[str]] | None:
    """Devolve (owner, repo, demais segmentos do path) de uma URL do GitHub, ou None."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https"):
        return None
    if (parts.hostname or "") not in GITHUB_HOSTS:
        return None
    segments = parts.path.strip("/").split("/")
    if len(segments) < 2:
        return None
    owner, repo = segments[0], segments[1]
    if repo.endswith(".git"):
        repo = repo[: -len(".git")]
    if not OWNER_PATTERN.fullmatch(owner) or owner.lower() in RESERVED_OWNERS:
        return None
    if not REPO_PATTERN.fullmatch(repo) or repo in (".", ".."):
        return None
    return owner, repo, segments[2:]


def repo_from_url(url: str) -> str | None:
    """`owner/name` de uma URL de repo do GitHub, ou None se não for um repo."""
    parsed = _github_segments(url)
    if parsed is None:
        return None
    owner, repo, _ = parsed
    return f"{owner}/{repo}"


def _distinct(repos: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for repo in repos:
        key = repo.lower()
        if key not in seen:
            seen.add(key)
            result.append(repo)
    return result


def linked_repos(urls: Iterable[str]) -> list[str]:
    """Repos distintos (sem diferenciar maiúsculas) citados nas URLs, na ordem de aparição."""
    repos = (repo_from_url(url) for url in urls)
    return _distinct(repo for repo in repos if repo is not None)


def pinned_repos(urls: Iterable[str]) -> list[str]:
    """Repos distintos cujas URLs apontam para `tree/factory/<...>`."""
    pinned: list[str] = []
    for url in urls:
        parsed = _github_segments(url)
        if parsed is None:
            continue
        owner, repo, rest = parsed
        if len(rest) >= 3 and rest[0] == "tree" and rest[1] == "factory" and rest[2]:
            pinned.append(f"{owner}/{repo}")
    return _distinct(pinned)


def pin_url(repo: str, identifier: str) -> str:
    """URL do pin da branch `factory/<identifier>` no repo."""
    return f"https://github.com/{repo}/tree/factory/{identifier}"


def parse_factory_config(text: str) -> str:
    """Lê o `test_command` do `.factory.toml` (sem espaços nas pontas)."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise FactoryConfigError(f"`.factory.toml` inválido: {error}") from error
    command = data.get("test_command")
    if not isinstance(command, str) or not command.strip():
        raise FactoryConfigError(
            "`.factory.toml` precisa de `test_command` (string não vazia)"
        )
    return command.strip()
