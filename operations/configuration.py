"""Configuration import and validation for the operations console.

The legacy profile and runtime files are import sources only.  This module
never writes to them; once an :class:`OpsStore` has a published version, that
version is the authoritative configuration.
"""

from __future__ import annotations

import copy
import json
import os
import re
from pathlib import Path
from typing import Any, Mapping


CAPABILITY_IDS = (
    "lark.read",
    "lark.write",
    "lark.personal",
    "file.read",
    "file.write",
    "p4.read",
    "p4.edit",
    "p4.submit",
)

CAPABILITY_DEFINITIONS = {
    "lark.read": ("飞书读取", "搜索和读取飞书资源"),
    "lark.write": ("飞书写入", "创建或更新飞书资源"),
    "lark.personal": ("飞书个人身份", "访问需要用户身份的飞书资源"),
    "file.read": ("本地文件读取", "读取配置工作区内的文件"),
    "file.write": ("本地文件写入", "修改配置工作区内的文件"),
    "p4.read": ("Perforce 读取", "查询 Perforce 工作区和历史"),
    "p4.edit": ("Perforce 编辑", "打开或新增 Perforce 文件"),
    "p4.submit": ("Perforce 提交", "同步或提交 Perforce 更改"),
}

TOP_LEVEL_KEYS = {
    "default_profile",
    "chat_profiles",
    "default_tier",
    "user_tiers",
    "admin_users",
    "user_identity_open_id",
    "profiles",
    "skills",
    "capabilities",
}
TIERS = {"read", "edit", "submit"}
MAX_CONFIG_BYTES = 2_000_000
MAX_PROMPT_CHARS = 100_000
MAX_INSTRUCTIONS_CHARS = 100_000
MAX_DESCRIPTION_CHARS = 4_000
MAX_NAME_CHARS = 200
MAX_PROFILE_TIMEOUT = 3_600
MAX_MAPPING_ITEMS = 10_000
MAX_REGISTRY_ITEMS = 500
_ID_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
_CREDENTIAL_RE = re.compile(
    r"(?i)(?:\bbearer\s+[a-z0-9._~+/=-]{4,}|"
    r"[\"']?(?:access[_-]?token|refresh[_-]?token|api[_-]?key|apikey|"
    r"client[_-]?secret|password|passwd|secret|authorization)[\"']?"
    r"\s*[:=]\s*[\"']?[^\s,;&}\]\"']+)"
)


class ConfigValidationError(ValueError):
    """Raised when an operations configuration cannot be published."""


def _fail(path: str, message: str) -> None:
    raise ConfigValidationError(f"{path}: {message}")


def _mapping(value: Any, path: str, limit: int = MAX_MAPPING_ITEMS) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        _fail(path, "must be an object")
    if len(value) > limit:
        _fail(path, f"must contain at most {limit} items")
    return value


def _string(value: Any, path: str, *, maximum: int, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        _fail(path, "must be a string")
    if not allow_empty and not value.strip():
        _fail(path, "must not be empty")
    if len(value) > maximum:
        _fail(path, f"must contain at most {maximum} characters")
    return value


def _identifier(value: Any, path: str) -> str:
    value = _string(value, path, maximum=128)
    if not _ID_PATTERN.fullmatch(value):
        _fail(path, "contains unsupported characters")
    return value


def _string_list(value: Any, path: str, *, limit: int = MAX_REGISTRY_ITEMS) -> list[str]:
    if not isinstance(value, list):
        _fail(path, "must be an array")
    if len(value) > limit:
        _fail(path, f"must contain at most {limit} items")
    result: list[str] = []
    for index, item in enumerate(value):
        item = _identifier(item, f"{path}[{index}]")
        if item in result:
            _fail(path, f"contains duplicate id {item!r}")
        result.append(item)
    return result


def _require_fields(value: Mapping[str, Any], expected: set[str], path: str) -> None:
    missing = expected - set(value)
    unknown = set(value) - expected
    if missing:
        _fail(path, f"missing fields: {', '.join(sorted(missing))}")
    if unknown:
        _fail(path, f"unknown fields: {', '.join(sorted(unknown))}")


def _reject_credentials(value: Any, path: str = "config") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            _reject_credentials(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_credentials(item, f"{path}[{index}]")
    elif isinstance(value, str) and _CREDENTIAL_RE.search(value):
        _fail(path, "credential-like content is not allowed in configuration")


def validate_config(config: Any) -> dict[str, Any]:
    """Validate and return a detached, JSON-compatible configuration."""

    root = _mapping(config, "config")
    _require_fields(root, TOP_LEVEL_KEYS, "config")
    try:
        encoded = json.dumps(root, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ConfigValidationError(f"config: must be JSON-compatible ({exc})") from exc
    if len(encoded) > MAX_CONFIG_BYTES:
        _fail("config", f"must be at most {MAX_CONFIG_BYTES} bytes")
    _reject_credentials(root)

    profiles = _mapping(root["profiles"], "profiles", MAX_REGISTRY_ITEMS)
    skills = _mapping(root["skills"], "skills", MAX_REGISTRY_ITEMS)
    capabilities = _mapping(root["capabilities"], "capabilities", len(CAPABILITY_IDS))
    if set(capabilities) != set(CAPABILITY_IDS):
        missing = set(CAPABILITY_IDS) - set(capabilities)
        unknown = set(capabilities) - set(CAPABILITY_IDS)
        details = []
        if missing:
            details.append(f"missing fixed ids: {', '.join(sorted(missing))}")
        if unknown:
            details.append(f"unknown ids: {', '.join(sorted(unknown))}")
        _fail("capabilities", "; ".join(details))

    for capability_id, capability in capabilities.items():
        _identifier(capability_id, f"capabilities[{capability_id!r}]")
        capability = _mapping(capability, f"capabilities.{capability_id}")
        _require_fields(capability, {"name", "description", "enabled"}, f"capabilities.{capability_id}")
        _string(capability["name"], f"capabilities.{capability_id}.name", maximum=MAX_NAME_CHARS)
        _string(
            capability["description"],
            f"capabilities.{capability_id}.description",
            maximum=MAX_DESCRIPTION_CHARS,
            allow_empty=True,
        )
        if not isinstance(capability["enabled"], bool):
            _fail(f"capabilities.{capability_id}.enabled", "must be a boolean")

    for skill_id, skill in skills.items():
        _identifier(skill_id, f"skills[{skill_id!r}]")
        skill = _mapping(skill, f"skills.{skill_id}")
        _require_fields(
            skill,
            {"name", "description", "instructions", "capabilities", "enabled"},
            f"skills.{skill_id}",
        )
        _string(skill["name"], f"skills.{skill_id}.name", maximum=MAX_NAME_CHARS)
        _string(
            skill["description"],
            f"skills.{skill_id}.description",
            maximum=MAX_DESCRIPTION_CHARS,
            allow_empty=True,
        )
        _string(
            skill["instructions"],
            f"skills.{skill_id}.instructions",
            maximum=MAX_INSTRUCTIONS_CHARS,
            allow_empty=True,
        )
        required_capabilities = _string_list(
            skill["capabilities"], f"skills.{skill_id}.capabilities"
        )
        unknown = set(required_capabilities) - set(CAPABILITY_IDS)
        if unknown:
            _fail(
                f"skills.{skill_id}.capabilities",
                f"unknown ids: {', '.join(sorted(unknown))}",
            )
        if not isinstance(skill["enabled"], bool):
            _fail(f"skills.{skill_id}.enabled", "must be a boolean")

    if not profiles:
        _fail("profiles", "must define at least one profile")
    for profile_id, profile in profiles.items():
        _identifier(profile_id, f"profiles[{profile_id!r}]")
        profile = _mapping(profile, f"profiles.{profile_id}")
        _require_fields(
            profile,
            {"name", "prompt", "skills", "capabilities", "timeout"},
            f"profiles.{profile_id}",
        )
        _string(profile["name"], f"profiles.{profile_id}.name", maximum=MAX_NAME_CHARS)
        _string(
            profile["prompt"],
            f"profiles.{profile_id}.prompt",
            maximum=MAX_PROMPT_CHARS,
            allow_empty=True,
        )
        profile_skills = _string_list(profile["skills"], f"profiles.{profile_id}.skills")
        profile_capabilities = _string_list(
            profile["capabilities"], f"profiles.{profile_id}.capabilities"
        )
        unknown_skills = set(profile_skills) - set(skills)
        unknown_capabilities = set(profile_capabilities) - set(CAPABILITY_IDS)
        if unknown_skills:
            _fail(
                f"profiles.{profile_id}.skills",
                f"unknown ids: {', '.join(sorted(unknown_skills))}",
            )
        if unknown_capabilities:
            _fail(
                f"profiles.{profile_id}.capabilities",
                f"unknown ids: {', '.join(sorted(unknown_capabilities))}",
            )
        required = {
            capability_id
            for skill_id in profile_skills
            for capability_id in skills[skill_id]["capabilities"]
        }
        missing_dependencies = required - set(profile_capabilities)
        if missing_dependencies:
            _fail(
                f"profiles.{profile_id}.capabilities",
                "missing skill dependencies: " + ", ".join(sorted(missing_dependencies)),
            )
        timeout = profile["timeout"]
        if isinstance(timeout, bool) or not isinstance(timeout, int):
            _fail(f"profiles.{profile_id}.timeout", "must be an integer")
        if not 1 <= timeout <= MAX_PROFILE_TIMEOUT:
            _fail(
                f"profiles.{profile_id}.timeout",
                f"must be between 1 and {MAX_PROFILE_TIMEOUT}",
            )

    default_profile = _identifier(root["default_profile"], "default_profile")
    if default_profile not in profiles:
        _fail("default_profile", f"unknown profile {default_profile!r}")

    chat_profiles = _mapping(root["chat_profiles"], "chat_profiles")
    for chat_id, profile_id in chat_profiles.items():
        _string(chat_id, "chat_profiles key", maximum=512)
        if profile_id not in profiles:
            _fail(f"chat_profiles.{chat_id}", f"unknown profile {profile_id!r}")

    default_tier = _string(root["default_tier"], "default_tier", maximum=32)
    if default_tier not in TIERS:
        _fail("default_tier", f"must be one of {', '.join(sorted(TIERS))}")
    user_tiers = _mapping(root["user_tiers"], "user_tiers")
    for user_id, tier in user_tiers.items():
        _string(user_id, "user_tiers key", maximum=512)
        if tier not in TIERS:
            _fail(f"user_tiers.{user_id}", f"must be one of {', '.join(sorted(TIERS))}")

    admin_users = _string_list(root["admin_users"], "admin_users", limit=MAX_MAPPING_ITEMS)
    for admin_user in admin_users:
        _string(admin_user, "admin_users item", maximum=512)
    _string(
        root["user_identity_open_id"],
        "user_identity_open_id",
        maximum=512,
        allow_empty=True,
    )

    return copy.deepcopy(dict(root))


def _capabilities_from_permissions(settings: Mapping[str, Any]) -> list[str]:
    permissions = settings.get("permissions", {})
    allowed = permissions.get("allow", []) if isinstance(permissions, dict) else []
    capabilities: set[str] = set()
    for permission in allowed:
        if not isinstance(permission, str):
            continue
        normalized = permission.strip().lower()
        if normalized in {"read", "grep", "glob"}:
            capabilities.add("file.read")
        elif normalized in {"edit", "write"}:
            capabilities.add("file.write")
        elif normalized.startswith("bash(lark-cli"):
            # The legacy allow surface was broad; execution policy will split it.
            capabilities.update({"lark.read", "lark.write", "lark.personal"})
        elif normalized.startswith("bash(p4 "):
            match = re.match(r"bash\(p4\s+([a-z0-9-]+)", normalized)
            verb = match.group(1) if match else ""
            if verb in {
                "info", "where", "files", "have", "status", "opened", "changes",
                "describe", "diff", "diff2", "annotate", "filelog",
            }:
                capabilities.add("p4.read")
            elif verb in {"edit", "add"}:
                capabilities.update({"p4.read", "p4.edit"})
            elif verb in {"submit", "sync"}:
                capabilities.update({"p4.read", "p4.edit", "p4.submit"})
    return [item for item in CAPABILITY_IDS if item in capabilities]


def _skill_capabilities(skill_id: str, authorized: list[str]) -> list[str]:
    # Dependencies come from a small built-in mapping. Skill prose is editable
    # content and must never become an authority-granting input.
    name = skill_id.rsplit(".", 1)[-1].lower()
    capabilities: set[str] = set()
    if name in {"p4", "p4v", "perforce"}:
        capabilities.update({"p4.read", "p4.edit", "p4.submit", "file.read", "file.write"})
    elif name in {"lark", "feishu", "lark-cli"}:
        capabilities.update({"lark.read", "lark.write", "lark.personal"})
    return [item for item in CAPABILITY_IDS if item in capabilities and item in authorized]


def _description(markdown: str, fallback: str) -> str:
    for raw_line in markdown.splitlines():
        line = raw_line.strip()
        if line and not line.startswith("#"):
            return line[:MAX_DESCRIPTION_CHARS]
    return fallback


def load_seed_config(
    profiles_dir: str | os.PathLike[str] | None = None,
    runtime_config_file: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Import current profile Markdown/settings and runtime JSON without writes."""

    package_dir = Path(__file__).resolve().parents[1]
    profile_root = Path(
        profiles_dir
        if profiles_dir is not None
        else os.environ.get("ASSISTANT_PROFILES_DIR", package_dir / "profiles")
    )
    runtime_path = Path(
        runtime_config_file
        if runtime_config_file is not None
        else os.environ.get(
            "ASSISTANT_RUNTIME_CONFIG_FILE",
            Path(os.environ.get("ASSISTANT_DATA_DIR", package_dir / "data"))
            / "runtime_config.json",
        )
    )
    runtime: dict[str, Any] = {}
    if runtime_path.is_file():
        try:
            loaded = json.loads(runtime_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigValidationError(f"runtime config cannot be read: {exc}") from exc
        if not isinstance(loaded, dict):
            raise ConfigValidationError("runtime config must contain an object")
        runtime = loaded

    skills: dict[str, dict[str, Any]] = {}
    profiles: dict[str, dict[str, Any]] = {}
    if profile_root.is_dir():
        for profile_path in sorted(path for path in profile_root.iterdir() if path.is_dir()):
            profile_id = profile_path.name
            if not _ID_PATTERN.fullmatch(profile_id):
                continue
            prompt_path = profile_path / "CLAUDE.md"
            prompt = prompt_path.read_text(encoding="utf-8") if prompt_path.is_file() else ""
            settings_path = profile_path / ".claude" / "settings.json"
            settings: dict[str, Any] = {}
            if settings_path.is_file():
                try:
                    loaded_settings = json.loads(settings_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    raise ConfigValidationError(
                        f"profile {profile_id!r} settings cannot be read: {exc}"
                    ) from exc
                if isinstance(loaded_settings, dict):
                    settings = loaded_settings
            profile_capabilities = _capabilities_from_permissions(settings)
            profile_skills: list[str] = []
            skill_root = profile_path / ".claude" / "skills"
            if skill_root.is_dir():
                for skill_path in sorted(path for path in skill_root.iterdir() if path.is_dir()):
                    instructions_path = skill_path / "SKILL.md"
                    if not instructions_path.is_file():
                        continue
                    skill_id = f"{profile_id}.{skill_path.name}"
                    if not _ID_PATTERN.fullmatch(skill_id):
                        continue
                    instructions = instructions_path.read_text(encoding="utf-8")
                    required = _skill_capabilities(skill_id, profile_capabilities)
                    skills[skill_id] = {
                        "name": skill_path.name.replace("-", " ").replace("_", " ").title(),
                        "description": _description(instructions, skill_path.name),
                        "instructions": instructions,
                        "capabilities": required,
                        "enabled": True,
                    }
                    profile_skills.append(skill_id)
                    profile_capabilities = [
                        item
                        for item in CAPABILITY_IDS
                        if item in set(profile_capabilities).union(required)
                    ]
            profiles[profile_id] = {
                "name": {"docs": "文档协作", "thesis": "毕设资料", "dev": "研发协作"}.get(
                    profile_id, profile_id
                ),
                "prompt": prompt,
                "skills": profile_skills,
                "capabilities": profile_capabilities,
                "timeout": 900 if profile_id == "dev" else int(os.environ.get("CLAUDE_TIMEOUT", "300")),
            }

    if not profiles:
        profiles["docs"] = {
            "name": "文档协作",
            "prompt": "",
            "skills": [],
            "capabilities": ["lark.read", "lark.write", "lark.personal"],
            "timeout": int(os.environ.get("CLAUDE_TIMEOUT", "300")),
        }

    default_profile = runtime.get("default_profile", "docs")
    if default_profile not in profiles:
        default_profile = "docs" if "docs" in profiles else next(iter(profiles))
    seed = {
        "default_profile": default_profile,
        "chat_profiles": runtime.get("chat_profiles", {}),
        "default_tier": runtime.get("default_tier", "read"),
        "user_tiers": runtime.get("user_tiers", {}),
        "admin_users": runtime.get("admin_users", []),
        "user_identity_open_id": runtime.get("user_identity_open_id", ""),
        "profiles": profiles,
        "skills": skills,
        "capabilities": {
            capability_id: {
                "name": CAPABILITY_DEFINITIONS[capability_id][0],
                "description": CAPABILITY_DEFINITIONS[capability_id][1],
                "enabled": True,
            }
            for capability_id in CAPABILITY_IDS
        },
    }
    return validate_config(seed)


# A concise alias for callers that treat the loader as a seed factory.
seed_config = load_seed_config


__all__ = [
    "CAPABILITY_IDS",
    "ConfigValidationError",
    "load_seed_config",
    "seed_config",
    "validate_config",
]
