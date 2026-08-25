#!/usr/bin/env python3
"""Run the production MCP stdio loop with explicit test backend dependencies."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "mcp" / "server.py"

TEST_ADAPTER_PATH_ENV = "APPLE_REMINDERS_TEST_HARNESS_ADAPTER_PATH"
TEST_EVENTKIT_BRIDGE_PATH_ENV = "APPLE_REMINDERS_TEST_HARNESS_EVENTKIT_BRIDGE_PATH"
TEST_DOCTOR_PATH_ENV = "APPLE_REMINDERS_TEST_HARNESS_DOCTOR_PATH"


def load_server() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "_apple_reminders_mcp_server_under_test",
        SERVER,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load MCP server: {SERVER}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def injected_path(environment_name: str, default: Path) -> Path:
    configured = os.environ.get(environment_name)
    return Path(configured).expanduser().resolve() if configured else default


def main() -> int:
    server = load_server()
    backend_paths = server.BackendPaths(
        adapter=injected_path(TEST_ADAPTER_PATH_ENV, server.DEFAULT_ADAPTER_PATH),
        eventkit_bridge=injected_path(
            TEST_EVENTKIT_BRIDGE_PATH_ENV,
            server.DEFAULT_EVENTKIT_BRIDGE_PATH,
        ),
        doctor=injected_path(TEST_DOCTOR_PATH_ENV, server.DEFAULT_DOCTOR_PATH),
    )
    return server.main(backend_paths=backend_paths)


if __name__ == "__main__":
    raise SystemExit(main())
