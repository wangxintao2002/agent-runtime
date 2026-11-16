#!/usr/bin/env python3
"""Start the installed Claude Code with a separate DeepSeek profile."""

import argparse
import getpass
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ENDPOINT = "https://api.deepseek.com/anthropic"
MODEL = "deepseek-flash[1m]"
PROFILE = Path.home() / ".local/state/agentos-next/claude-deepseek"


def child_environment(key):
    # Never fall back to an unrelated provider credential or model setting.
    env = {
        name: value for name, value in os.environ.items()
        if not name.startswith(("ANTHROPIC_", "CLAUDE_CODE_", "CLAUDE_AGENT_"))
        and name not in {"CLAUDE_CONFIG_DIR", "DEEPSEEK_API_KEY", "DS_API_KEY"}
    }
    env.update({
        "CLAUDE_CONFIG_DIR": str(PROFILE),
        "ANTHROPIC_BASE_URL": ENDPOINT,
        "ANTHROPIC_AUTH_TOKEN": key,
        "ANTHROPIC_MODEL": MODEL,
        "ANTHROPIC_DEFAULT_OPUS_MODEL": MODEL,
        "ANTHROPIC_DEFAULT_SONNET_MODEL": MODEL,
        "ANTHROPIC_DEFAULT_FABLE_MODEL": MODEL,
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": "deepseek-flash",
        "CLAUDE_CODE_SUBAGENT_MODEL": "deepseek-flash",
        "CLAUDE_CODE_EFFORT_LEVEL": "max",
        "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "786432",
    })
    return env


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="local checks only; no API request")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("claude_args", nargs=argparse.REMAINDER, help="Claude arguments after --")
    args = parser.parse_args()
    binary = shutil.which("claude")
    if binary is None:
        parser.error("找不到 claude；请先安装 Claude Code。")
    workspace = args.workspace.expanduser().resolve()
    if not workspace.is_dir():
        parser.error(f"工作目录不存在：{workspace}")
    forwarded = args.claude_args
    if forwarded[:1] == ["--"]:
        forwarded = forwarded[1:]
    command = [binary, "--setting-sources", "", "--strict-mcp-config",
               "--mcp-config", '{"mcpServers":{}}', "--model", MODEL]
    key = (os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("DS_API_KEY") or "").strip()
    if args.check:
        result = subprocess.run(command + ["--version"], env=child_environment(""),
                                cwd=workspace, capture_output=True, text=True, check=True)
        print(json.dumps({
            "claude": result.stdout.strip(), "endpoint": ENDPOINT, "model": MODEL,
            "workspace": str(workspace), "profile": str(PROFILE),
            "deepseek_key_present": bool(key), "api_request_made": False,
        }, ensure_ascii=False, indent=2))
        return
    if not key:
        if not sys.stdin.isatty():
            parser.error("请在交互终端输入 Key，或在本机设置 DEEPSEEK_API_KEY。")
        try:
            key = getpass.getpass("DeepSeek API Key（隐藏输入，不保存）：").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已取消。", file=sys.stderr)
            return 1
    if not key:
        parser.error("DeepSeek API Key 不能为空。")
    PROFILE.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chdir(workspace)
    os.execvpe(binary, command + forwarded, child_environment(key))


if __name__ == "__main__":
    sys.exit(main())
