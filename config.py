"""Small, explicit host and terminal profiles; no credentials belong here."""

import json
import os
from pathlib import Path
import re

HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
RGB = re.compile(r"#[0-9a-fA-F]{6}\Z")


class ConfigError(ValueError):
    pass


def config_path():
    base = os.environ.get("XDG_CONFIG_HOME")
    if base and not Path(base).is_absolute():
        raise ConfigError("XDG_CONFIG_HOME must be absolute")
    return (Path(base) if base else Path.home() / ".config") / "mosherdr/config.json"


def validate_host(host):
    if not isinstance(host, str) or not HOST.fullmatch(host):
        raise ConfigError("use a plain SSH host alias, containing letters, digits, dots, underscores, or hyphens")
    return host


def validate_colors(profile):
    if not isinstance(profile, dict):
        raise ConfigError("colors must be an object")
    if set(profile) - {"enabled", "foreground", "background"}:
        raise ConfigError("unknown color setting")
    enabled = profile.get("enabled", False)
    if type(enabled) is not bool:
        raise ConfigError("colors.enabled must be true or false")
    for name in ("foreground", "background"):
        value = profile.get(name)
        if (enabled or value is not None) and (not isinstance(value, str) or not RGB.fullmatch(value)):
            raise ConfigError("color profiles require foreground and background in #RRGGBB form")
    return dict(profile)


def validate(data):
    if not isinstance(data, dict) or set(data) - {"version", "hosts", "colors"}:
        raise ConfigError("invalid mosherdr configuration")
    if type(data.get("version", 1)) is not int or data.get("version", 1) != 1:
        raise ConfigError("unsupported configuration version")
    hosts = data.get("hosts", {})
    if not isinstance(hosts, dict):
        raise ConfigError("hosts must be an object")
    for host, options in hosts.items():
        validate_host(host)
        if not isinstance(options, dict) or set(options) - {"colors"}:
            raise ConfigError("invalid host settings")
        if "colors" in options:
            validate_colors(options["colors"])
    if "colors" in data:
        validate_colors(data["colors"])
    return data


def load_config(path=None):
    path = Path(path) if path is not None else config_path()
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return {"version": 1, "hosts": {}}
    if len(data) > 65536:
        raise ConfigError("configuration exceeded size limit")
    try:
        return validate(json.loads(data))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigError("configuration is not valid JSON") from exc


def configured_host(host):
    validate_host(host)
    return host in load_config().get("hosts", {})
