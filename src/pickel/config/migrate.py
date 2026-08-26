"""旧 config.yaml → 分层 settings / models / auth + agents + 会话库。

加载 yaml 时不展开 ${ENV}，写回保留字面量。
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import yaml

from pickel.config.paths import home_dir

# openviking：策略进 settings 的 extensions.openviking，密钥进 auth 的 extensions.openviking
_OPENVIKING_SECRET_KEYS = frozenset({"base_url", "account_id", "user_id", "user_key"})
_OPENVIKING_STRATEGY_KEYS = frozenset(
    {
        "enabled",
        "timeout_seconds",
        "commit_after_minutes",
        "commit_after_turns",
        "tool_output_max_chars",
        "session_recall",
        "agents",
    }
)
_MODEL_SECRET_KEYS = frozenset({"api_key", "api_base"})
_AGENT_YAML_FIELDS = (
    "workspace_path",
    "tools",
    "extensions",
    "file_access_mode",
    "models",
    "remote_agent_id",
    "skills_path",
    "behavior_path",
)
_LEGACY_SESSIONS_DIR = ".pickel"
_SESSIONS_DB = "sessions.db"
_AUTH_MODE = 0o600


def migrate_from_yaml(
    source: Path,
    *,
    home: Path | None = None,
    project_root: Path | None = None,
) -> dict[str, Any]:
    """从旧 config.yaml 迁移到分层配置与全局会话库。

    返回摘要 dict（路径、agents、sessions 动作、warnings）。
    """
    source = Path(source)
    if not source.is_file():
        raise FileNotFoundError(f"Config file not found: {source}")

    resolved_home = Path(home) if home is not None else home_dir()
    resolved_project = (
        Path(project_root) if project_root is not None else source.resolve().parent
    )
    resolved_home.mkdir(parents=True, exist_ok=True)

    with source.open(encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"config.yaml 须为对象: {source}")

    warnings: list[str] = []
    summary: dict[str, Any] = {
        "source": str(source.resolve()),
        "home": str(resolved_home),
        "project_root": str(resolved_project.resolve()),
        "warnings": warnings,
    }

    settings = _build_settings(raw, project_root=resolved_project)
    models, provider_secrets = _split_providers(raw.get("providers") or {})
    auth_from_yaml = _build_auth(provider_secrets, raw.get("openviking"))

    settings_file = resolved_home / "settings.json"
    models_file = resolved_home / "models.json"
    auth_file = resolved_home / "auth.json"

    _write_json(settings_file, settings)
    _write_json(models_file, models)
    auth_written = _merge_write_auth(auth_file, auth_from_yaml)
    summary["settings"] = str(settings_file)
    summary["models"] = str(models_file)
    summary["auth"] = str(auth_file)
    summary["auth_merged"] = auth_written["merged"]
    summary["auth_skipped_keys"] = auth_written["skipped_keys"]

    agents_written = _write_agents(
        agents=raw.get("agents") or {},
        project_root=resolved_project,
    )
    summary["agents"] = agents_written

    sessions_info = _migrate_sessions(
        project_root=resolved_project,
        home=resolved_home,
        warnings=warnings,
    )
    summary["sessions"] = sessions_info

    bak_path = _backup_config_yaml(source)
    summary["config_backup"] = str(bak_path) if bak_path else None

    return summary


def _build_settings(raw: dict[str, Any], *, project_root: Path) -> dict[str, Any]:
    settings: dict[str, Any] = {}
    for key in (
        "default_agent",
        "default_llm",
        "default_file_access_mode",
        "react_max_steps",
        "context_cli_turn_window",
        "observability",
    ):
        if key in raw and raw[key] is not None:
            settings[key] = raw[key]

    if "trace_enabled" in raw and "observability" not in settings:
        settings["observability"] = {
            "trace": {"mode": "standard" if bool(raw["trace_enabled"]) else "off"}
        }

    skills = raw.get("default_skills_path")
    if skills is not None:
        settings["default_skills_path"] = _prefer_relative_path(
            skills, project_root=project_root
        )

    ov = raw.get("openviking")
    if isinstance(ov, dict):
        strategy = {k: v for k, v in ov.items() if k in _OPENVIKING_STRATEGY_KEYS}
        if strategy:
            settings.setdefault("extensions", {})["openviking"] = strategy

    return settings


def _split_providers(
    providers: Any,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """拆 providers：models.json 无密钥；auth 侧 provider 级 api_key/api_base。"""
    if not isinstance(providers, dict):
        return {"providers": {}}, {}

    clean_providers: dict[str, Any] = {}
    secrets: dict[str, dict[str, Any]] = {}

    for provider_id, catalog in providers.items():
        if not isinstance(catalog, dict):
            continue
        models_in = catalog.get("models") or {}
        if not isinstance(models_in, dict):
            continue
        clean_models: dict[str, Any] = {}
        provider_secret: dict[str, Any] = {}
        for model_id, model_cfg in models_in.items():
            if not isinstance(model_cfg, dict):
                clean_models[model_id] = model_cfg
                continue
            cleaned = {
                k: v for k, v in model_cfg.items() if k not in _MODEL_SECRET_KEYS
            }
            clean_models[model_id] = cleaned
            for secret_key in _MODEL_SECRET_KEYS:
                if secret_key in model_cfg and model_cfg[secret_key] is not None:
                    # 同 provider 多个 model 有密钥时保留首次
                    if secret_key not in provider_secret:
                        provider_secret[secret_key] = model_cfg[secret_key]
        clean_providers[provider_id] = {"models": clean_models}
        if provider_secret:
            secrets[str(provider_id)] = provider_secret

    return {"providers": clean_providers}, secrets


def _build_auth(
    provider_secrets: dict[str, dict[str, Any]],
    openviking: Any,
) -> dict[str, Any]:
    auth: dict[str, Any] = {"providers": dict(provider_secrets)}
    if isinstance(openviking, dict):
        ov_secrets = {
            k: v
            for k, v in openviking.items()
            if k in _OPENVIKING_SECRET_KEYS and v is not None
        }
        if ov_secrets:
            auth.setdefault("extensions", {})["openviking"] = ov_secrets
    return auth


def _merge_write_auth(path: Path, incoming: dict[str, Any]) -> dict[str, Any]:
    """写入 auth.json；已存在则合并不覆盖已有密钥键。"""
    skipped: list[str] = []
    merged = False
    if path.is_file():
        merged = True
        try:
            existing = json.loads(path.read_text(encoding="utf-8")) or {}
        except json.JSONDecodeError:
            existing = {}
        if not isinstance(existing, dict):
            existing = {}
        result = _deep_merge_auth(existing, incoming, skipped_keys=skipped, prefix="")
    else:
        result = incoming

    _write_json(path, result)
    try:
        os.chmod(path, _AUTH_MODE)
    except OSError:
        pass
    return {"merged": merged, "skipped_keys": skipped}


def _deep_merge_auth(
    existing: dict[str, Any],
    incoming: dict[str, Any],
    *,
    skipped_keys: list[str],
    prefix: str,
) -> dict[str, Any]:
    """dict 递归合并；叶子密钥键已存在则跳过。"""
    out = dict(existing)
    for key, value in incoming.items():
        path_key = f"{prefix}.{key}" if prefix else str(key)
        if key not in out:
            out[key] = value
            continue
        if isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _deep_merge_auth(
                out[key], value, skipped_keys=skipped_keys, prefix=path_key
            )
        else:
            # 已有非空值不覆盖
            if out[key] is not None and out[key] != "":
                skipped_keys.append(path_key)
            else:
                out[key] = value
    return out


def _write_agents(
    *,
    agents: Any,
    project_root: Path,
) -> list[dict[str, str]]:
    written: list[dict[str, str]] = []
    if not isinstance(agents, dict):
        return written

    for agent_id, agent_cfg in agents.items():
        if not isinstance(agent_cfg, dict):
            continue
        agent_dir = project_root / "agents" / str(agent_id)
        agent_dir.mkdir(parents=True, exist_ok=True)

        # 不碰 AGENT.md
        yaml_body: dict[str, Any] = {}
        legacy_llm = agent_cfg.get("llm")
        if isinstance(legacy_llm, dict) and "models" not in agent_cfg:
            yaml_body["models"] = {"primary": legacy_llm}
        for field in _AGENT_YAML_FIELDS:
            if field not in agent_cfg or agent_cfg[field] is None:
                continue
            value = agent_cfg[field]
            if field in ("workspace_path", "skills_path", "behavior_path"):
                value = _prefer_relative_path(value, project_root=project_root)
            # 默认 behavior 即 agents/<id> 时省略，与 scan 默认一致
            if field == "behavior_path":
                default_rel = f"agents/{agent_id}"
                if str(value).replace("\\", "/") in (default_rel, str(agent_dir)):
                    continue
            yaml_body[field] = value

        yaml_path = agent_dir / "agent.yaml"
        with yaml_path.open("w", encoding="utf-8") as handle:
            yaml.safe_dump(
                yaml_body,
                handle,
                allow_unicode=True,
                default_flow_style=False,
                sort_keys=False,
            )
        written.append({"id": str(agent_id), "path": str(yaml_path)})

    return written


def _migrate_sessions(
    *,
    project_root: Path,
    home: Path,
    warnings: list[str],
) -> dict[str, Any]:
    """项目旁 .pickel/sessions.db → 全局 home/sessions.db。"""
    legacy = project_root / _LEGACY_SESSIONS_DIR / _SESSIONS_DB
    global_db = Path(home) / _SESSIONS_DB

    info: dict[str, Any] = {
        "legacy": str(legacy) if legacy.is_file() else None,
        "global": str(global_db),
        "action": "skipped",
    }
    if not legacy.is_file():
        info["action"] = "no_legacy"
        return info

    if not global_db.exists():
        global_db.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(legacy, global_db)
        _fill_missing_cwd(global_db, project_root=project_root, warnings=warnings)
        bak = _rename_to_bak(legacy)
        info["action"] = "copied"
        info["legacy_bak"] = str(bak) if bak else None
        warnings.append("已复制项目 sessions.db 到全局；若 schema 非 v3 可能需重建")
        return info

    # 全局库已存在：尝试导入补 cwd；失败则备份说明
    try:
        imported = _import_sessions(
            source=legacy, target=global_db, project_root=project_root
        )
        bak = _rename_to_bak(legacy)
        info["action"] = "imported"
        info["imported_sessions"] = imported
        info["legacy_bak"] = str(bak) if bak else None
    except Exception as exc:  # noqa: BLE001 — 迁移兜底
        warnings.append(f"会话导入失败，保留全局库: {exc}")
        bak = _rename_to_bak(legacy)
        info["action"] = "import_failed"
        info["legacy_bak"] = str(bak) if bak else None

    return info


def _fill_missing_cwd(
    db_path: Path, *, project_root: Path, warnings: list[str]
) -> None:
    """为缺 cwd 的 session 行填 project_root。schema 不符则记 warning。"""
    cwd_value = str(project_root.resolve())
    try:
        with sqlite3.connect(db_path) as conn:
            cols = {
                row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()
            }
            if "sessions" not in {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }:
                warnings.append("全局 sessions.db 无 sessions 表，跳过 cwd 回填")
                return
            if "cwd" not in cols:
                warnings.append("sessions 表无 cwd 列（非 v3 schema），跳过 cwd 回填")
                return
            conn.execute(
                """
                UPDATE sessions
                SET cwd = ?
                WHERE cwd IS NULL OR cwd = ''
                """,
                (cwd_value,),
            )
            conn.commit()
    except sqlite3.Error as exc:
        warnings.append(f"cwd 回填失败: {exc}")


def _import_sessions(*, source: Path, target: Path, project_root: Path) -> int:
    """把 source 中 sessions/entries 导入 target（同 id 跳过）。返回导入 session 数。"""
    cwd_value = str(project_root.resolve())
    imported = 0
    with sqlite3.connect(source) as src, sqlite3.connect(target) as dst:
        src.row_factory = sqlite3.Row
        src_tables = {
            r[0]
            for r in src.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "sessions" not in src_tables:
            return 0
        src_cols = {
            row[1] for row in src.execute("PRAGMA table_info(sessions)").fetchall()
        }
        dst_cols = {
            row[1] for row in dst.execute("PRAGMA table_info(sessions)").fetchall()
        }
        if "session_id" not in src_cols or "session_id" not in dst_cols:
            raise ValueError("sessions 表缺少 session_id")

        existing = {
            r[0] for r in dst.execute("SELECT session_id FROM sessions").fetchall()
        }
        session_rows = src.execute("SELECT * FROM sessions").fetchall()
        for row in session_rows:
            sid = row["session_id"]
            if sid in existing:
                continue
            data = dict(row)
            if "cwd" in dst_cols:
                if not data.get("cwd"):
                    data["cwd"] = cwd_value
            # 只写目标有的列
            cols = [c for c in data.keys() if c in dst_cols]
            placeholders = ", ".join("?" for _ in cols)
            col_names = ", ".join(cols)
            dst.execute(
                f"INSERT INTO sessions ({col_names}) VALUES ({placeholders})",
                [data[c] for c in cols],
            )
            imported += 1

            if "session_entries" in src_tables and "session_entries" in {
                r[0]
                for r in dst.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }:
                entry_rows = src.execute(
                    "SELECT * FROM session_entries WHERE session_id = ?",
                    (sid,),
                ).fetchall()
                dst_entry_cols = {
                    row[1]
                    for row in dst.execute(
                        "PRAGMA table_info(session_entries)"
                    ).fetchall()
                }
                for entry in entry_rows:
                    edata = dict(entry)
                    ecols = [c for c in edata.keys() if c in dst_entry_cols]
                    eph = ", ".join("?" for _ in ecols)
                    enames = ", ".join(ecols)
                    try:
                        dst.execute(
                            f"INSERT INTO session_entries ({enames}) VALUES ({eph})",
                            [edata[c] for c in ecols],
                        )
                    except sqlite3.IntegrityError:
                        pass
        dst.commit()
    return imported


def _rename_to_bak(path: Path) -> Path | None:
    bak = path.with_suffix(path.suffix + ".bak")
    if bak.exists():
        # 已有 bak：再加后缀避免丢数据
        n = 1
        while True:
            candidate = path.with_suffix(path.suffix + f".bak.{n}")
            if not candidate.exists():
                bak = candidate
                break
            n += 1
    path.rename(bak)
    return bak


def _backup_config_yaml(source: Path) -> Path | None:
    """备份 config.yaml → config.yaml.bak（复制，保留源以便可重复迁移）。"""
    bak = source.with_name(source.name + ".bak")
    shutil.copy2(source, bak)
    return bak


def _prefer_relative_path(value: Any, *, project_root: Path) -> Any:
    if value is None:
        return None
    text = str(value)
    path = Path(text)
    root = project_root.resolve()
    try:
        if path.is_absolute():
            return str(path.resolve().relative_to(root))
    except ValueError:
        return text
    return text


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
