from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from kefu_tool_backend.tool_backend_service import ToolBackendService


class AdminToolBoundaryTestCase(unittest.TestCase):
    @staticmethod
    def backend(**overrides: object) -> ToolBackendService:
        options: dict[str, object] = {
            "identity_base_url": "",
            "identity_org_id": "",
            "identity_room_name": "",
            "identity_api_key": "",
            "kuaimai_client": SimpleNamespace(),
            "qwen_vision_client": SimpleNamespace(enabled=False),
            "timeout_sec": 1,
            "admin_tools_enabled": False,
            "file_read_enabled": False,
            "shell_exec_enabled": False,
            "file_read_roots": "",
            "shell_allowed_commands": "",
        }
        options.update(overrides)
        return ToolBackendService(**options)  # type: ignore[arg-type]

    @staticmethod
    def call(service: ToolBackendService, tool_name: str, **args: object) -> dict[str, object]:
        return service.handle(
            tool_name=tool_name,
            payload={"args": args, "event": {"role": "admin"}},
        ).as_dict()

    def test_both_tools_are_disabled_by_default(self) -> None:
        service = self.backend(file_read_roots="*", shell_allowed_commands="*")

        self.assertEqual(
            self.call(service, "file_read", path="/etc/hosts")["message"],
            "admin_tools_disabled",
        )
        self.assertEqual(
            self.call(service, "shell_exec", command="true")["message"],
            "admin_tools_disabled",
        )

    def test_enabling_one_tool_does_not_enable_the_other(self) -> None:
        file_only = self.backend(
            file_read_enabled=True,
            file_read_roots="*",
            shell_allowed_commands="*",
        )
        shell_only = self.backend(
            shell_exec_enabled=True,
            file_read_roots="*",
            shell_allowed_commands="*",
        )

        self.assertEqual(
            self.call(file_only, "shell_exec", command="true")["message"],
            "admin_tools_disabled",
        )
        self.assertEqual(
            self.call(shell_only, "file_read", path="/etc/hosts")["message"],
            "admin_tools_disabled",
        )

    def test_enabled_tools_fail_closed_without_allowlists(self) -> None:
        service = self.backend(file_read_enabled=True, shell_exec_enabled=True)

        self.assertEqual(
            self.call(service, "file_read", path="/etc/hosts")["message"],
            "file_read_roots_not_configured",
        )
        self.assertEqual(
            self.call(service, "shell_exec", command="true")["message"],
            "shell_allowed_commands_not_configured",
        )

    def test_file_read_allows_only_resolved_paths_below_roots(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dxl-file-boundary-") as directory:
            base = Path(directory)
            allowed = base / "allowed"
            sibling = base / "allowed-sibling"
            allowed.mkdir()
            sibling.mkdir()
            inside = allowed / "inside.txt"
            outside = sibling / "outside.txt"
            inside.write_text("inside\nsecond", encoding="utf-8")
            outside.write_text("outside", encoding="utf-8")
            service = self.backend(file_read_enabled=True, file_read_roots=str(allowed))

            accepted = self.call(service, "file_read", path=str(inside), line_count=1)
            sibling_denied = self.call(service, "file_read", path=str(outside))
            traversal_denied = self.call(
                service,
                "file_read",
                path=str(allowed / ".." / sibling.name / outside.name),
            )

            self.assertTrue(accepted["ok"])
            self.assertEqual(accepted["data"]["content"], "inside")  # type: ignore[index]
            self.assertEqual(sibling_denied["message"], "file_read_path_denied")
            self.assertEqual(traversal_denied["message"], "file_read_path_denied")

    def test_file_read_rejects_symlink_escape(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dxl-file-symlink-") as directory:
            base = Path(directory)
            allowed = base / "allowed"
            allowed.mkdir()
            outside = base / "outside.txt"
            outside.write_text("outside", encoding="utf-8")
            link = allowed / "outside-link.txt"
            try:
                link.symlink_to(outside)
            except OSError as exc:
                self.skipTest(f"symlink unavailable: {exc}")
            service = self.backend(file_read_enabled=True, file_read_roots=str(allowed))

            result = self.call(service, "file_read", path=str(link))

            self.assertEqual(result["message"], "file_read_path_denied")

    def test_file_read_wildcard_explicitly_allows_any_file(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dxl-file-wildcard-") as directory:
            target = Path(directory) / "outside.txt"
            target.write_text("allowed by wildcard", encoding="utf-8")
            service = self.backend(file_read_enabled=True, file_read_roots="*")

            result = self.call(service, "file_read", path=str(target))

            self.assertTrue(result["ok"])
            self.assertEqual(result["data"]["content"], "allowed by wildcard")  # type: ignore[index]

    def test_shell_allows_exact_and_safe_prefix_but_rejects_injection(self) -> None:
        rules = json.dumps(["git status", "printf safe | wc -c"])
        service = self.backend(shell_exec_enabled=True, shell_allowed_commands=rules)
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="ok",
            stderr="",
        )

        with patch(
            "kefu_tool_backend.tool_backend_service.subprocess.run",
            return_value=completed,
        ) as run:
            prefix = self.call(service, "shell_exec", command="git status --short")
            exact_shell = self.call(service, "shell_exec", command="printf safe | wc -c")
            boundary = self.call(service, "shell_exec", command="git statusx")
            chain = self.call(service, "shell_exec", command="git status; id")
            substitution = self.call(service, "shell_exec", command="git status $(id)")

        self.assertTrue(prefix["ok"])
        self.assertTrue(exact_shell["ok"])
        self.assertEqual(prefix["data"]["allowed_by"], "git status")  # type: ignore[index]
        self.assertEqual(boundary["message"], "shell_command_denied")
        self.assertEqual(chain["message"], "shell_command_denied")
        self.assertEqual(substitution["message"], "shell_command_denied")
        self.assertEqual(run.call_count, 2)

    def test_shell_prefix_keeps_quoted_argument_punctuation_usable(self) -> None:
        service = self.backend(shell_exec_enabled=True, shell_allowed_commands='["curl"]')
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="ok", stderr="")
        command = 'curl "https://example.test/path?a=1&b=2"'

        with patch(
            "kefu_tool_backend.tool_backend_service.subprocess.run",
            return_value=completed,
        ) as run:
            result = self.call(service, "shell_exec", command=command)

        self.assertTrue(result["ok"])
        self.assertEqual(run.call_args.args[0], command)

    def test_shell_wildcard_explicitly_allows_arbitrary_shell(self) -> None:
        service = self.backend(shell_exec_enabled=True, shell_allowed_commands="*")
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="ok", stderr="")
        command = "printf ok | wc -c; true"

        with patch(
            "kefu_tool_backend.tool_backend_service.subprocess.run",
            return_value=completed,
        ) as run:
            result = self.call(service, "shell_exec", command=command)

        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["allowed_by"], "*")  # type: ignore[index]
        self.assertEqual(run.call_args.args[0], command)

    def test_environment_flags_and_allowlists_are_loaded_independently(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dxl-file-env-") as directory:
            target = Path(directory) / "report.txt"
            target.write_text("report", encoding="utf-8")
            env = {
                "CLAWBOT_ENABLE_FILE_READ_TOOL": "1",
                "CLAWBOT_ENABLE_SHELL_EXEC_TOOL": "0",
                "CLAWBOT_FILE_READ_ROOTS": directory,
                "CLAWBOT_SHELL_ALLOWED_COMMANDS": "*",
            }
            with patch.dict(os.environ, env, clear=True):
                service = ToolBackendService(
                    identity_base_url="",
                    identity_org_id="",
                    identity_room_name="",
                    identity_api_key="",
                    kuaimai_client=SimpleNamespace(),
                    qwen_vision_client=SimpleNamespace(enabled=False),
                    timeout_sec=1,
                )

            self.assertTrue(self.call(service, "file_read", path=str(target))["ok"])
            self.assertEqual(
                self.call(service, "shell_exec", command="true")["message"],
                "admin_tools_disabled",
            )

    def test_legacy_master_switch_does_not_bypass_allowlists(self) -> None:
        service = self.backend(
            admin_tools_enabled=True,
            file_read_enabled=False,
            shell_exec_enabled=False,
        )

        self.assertEqual(
            self.call(service, "file_read", path="/etc/hosts")["message"],
            "file_read_roots_not_configured",
        )
        self.assertEqual(
            self.call(service, "shell_exec", command="true")["message"],
            "shell_allowed_commands_not_configured",
        )


if __name__ == "__main__":
    unittest.main()
