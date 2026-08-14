from __future__ import annotations

import base64
import json
import mimetypes
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from .models import IncomingEvent

DEFAULT_ATTACHMENT_MAX_BYTES = 5_000_000
MAX_GATEWAY_PARAMS_ARG_BYTES = 100_000
GATEWAY_NODE_EVAL = """
import fs from 'node:fs/promises';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

async function readStdin() {
  const chunks = [];
  for await (const chunk of process.stdin) {
    chunks.push(typeof chunk === 'string' ? Buffer.from(chunk) : chunk);
  }
  return Buffer.concat(chunks).toString('utf8');
}

async function resolveGatewayRpc(packageRoot) {
  if (!packageRoot) {
    throw new Error('missing openclawPackageRoot');
  }
  const distDir = path.join(packageRoot, 'dist');
  const entries = (await fs.readdir(distDir))
    .filter((entry) => /^gateway-rpc-.*\\.js$/.test(entry))
    .sort();
  for (const entry of entries) {
    const mod = await import(pathToFileURL(path.join(distDir, entry)).href);
    if (typeof mod.n === 'function') {
      return mod.n;
    }
  }
  throw new Error(`gateway-rpc module not found under ${distDir}`);
}

async function main() {
  const raw = await readStdin();
  const request = JSON.parse(raw || '{}');
  const callGatewayFromCli = await resolveGatewayRpc(String(request.openclawPackageRoot || '').trim());
  const timeoutMs = Number(request.timeoutMs || 30000);
  const result = await callGatewayFromCli(
    String(request.method || '').trim() || 'agent',
    {
      json: true,
      timeout: String(timeoutMs),
      expectFinal: Boolean(request.expectFinal),
      ...(request.url ? { url: String(request.url) } : {}),
      ...(request.token ? { token: String(request.token) } : {}),
    },
    request.params ?? {},
    {
      expectFinal: Boolean(request.expectFinal),
      progress: false,
    },
  );
  process.stdout.write(`${JSON.stringify(result)}\\n`);
}

main().catch((err) => {
  process.stderr.write(`${err?.stack || String(err)}\\n`);
  process.exit(1);
});
"""


@dataclass
class OpenClawReply:
    text: str
    session_key: str


def build_default_reply_prompt(event: IncomingEvent) -> str:
    text = (event.text or "").strip()
    raw = event.raw if isinstance(event.raw, dict) else {}
    media_path = str(raw.get("media_path") or "").strip()
    customer = (event.platform_nickname or "").strip() or "顾客"
    store_name = (event.store_name or "").strip() or event.store_id

    if str(event.message_type or "").strip().lower() == "image":
        customer_message = (
            "顾客发送了图片消息。\n"
            f"媒体地址: {media_path or '（无）'}\n"
            f"附带文字: {text if text else '(无文字)'}"
        )
    else:
        customer_message = text if text else "(空消息)"

    return (
        "你是电商店铺客服，请生成可直接发给顾客的中文回复。\n"
        "要求：\n"
        "1) 只输出最终回复文本，不要解释、不要JSON、不要Markdown。\n"
        "2) 语气礼貌、简洁，1-3句。\n"
        "3) 信息不足时，明确追问订单号/规格/时间等关键字段。\n"
        "4) 不要承诺超出权限的赔付金额。\n\n"
        f"平台: {event.platform}\n"
        f"店铺: {store_name}\n"
        f"平台昵称: {customer}\n"
        f"消息类型: {event.message_type}\n"
        f"顾客消息:\n{customer_message}\n"
    )


def parse_json_output(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    if not text:
        raise ValueError("empty_openclaw_output")

    decoder = json.JSONDecoder()
    candidates: list[dict[str, Any]] = []
    for idx, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            payload, _ = decoder.raw_decode(text[idx:])
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            candidates.append(payload)

    if not candidates:
        raise ValueError("openclaw_json_not_found")

    # Prefer payloads that contain the assistant result structure.
    for payload in reversed(candidates):
        entries = payload.get("payloads")
        if isinstance(entries, list):
            return payload
        result = payload.get("result")
        if isinstance(result, dict) and isinstance(result.get("payloads"), list):
            return payload

    return candidates[-1]


def extract_reply_text(payload: dict[str, Any]) -> str:
    entries = payload.get("payloads")
    if not isinstance(entries, list):
        result = payload.get("result")
        if isinstance(result, dict):
            entries = result.get("payloads")
    if not isinstance(entries, list):
        return ""

    candidates: list[str] = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if text:
            candidates.append(text)

    if not candidates:
        return ""

    # Prefer the latest assistant-style text and avoid leaking internal tool/exec errors.
    for text in reversed(candidates):
        if not _looks_like_internal_error_text(text):
            return text
    return candidates[-1]


def _looks_like_internal_error_text(text: str) -> bool:
    value = (text or "").strip()
    if not value:
        return False
    patterns = (
        r"Traceback \(most recent call last\):",
        r"Command exited with code\s+\d+",
        r"^\s*⚠️?\s*🛠️?\s*Exec:",
        r"\"tool\"\s*:\s*\"exec\"",
        r"\"status\"\s*:\s*\"error\"",
        r"/bin/bash:",
        r"OperationalError:",
        r"RuntimeError:",
    )
    return any(re.search(p, value, flags=re.IGNORECASE | re.MULTILINE) for p in patterns)


def looks_like_json_reply(text: str) -> bool:
    value = (text or "").strip()
    if not value:
        return False

    if value.startswith("```") and value.endswith("```"):
        lines = value.splitlines()
        if len(lines) >= 2:
            lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            value = "\n".join(lines).strip()
            if not value:
                return False

    is_json_container = (
        (value.startswith("{") and value.endswith("}"))
        or (value.startswith("[") and value.endswith("]"))
    )
    if not is_json_container:
        return False

    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return False
    return isinstance(payload, (dict, list))


def resolve_event_media_paths(event: IncomingEvent) -> list[str]:
    raw = event.raw if isinstance(event.raw, dict) else {}
    candidates: list[Any] = [raw.get("media_path")]
    raw_media_urls = raw.get("media_urls")
    if isinstance(raw_media_urls, list):
        candidates.extend(raw_media_urls)
    candidates.append(event.media_url)
    seen: set[str] = set()
    resolved: list[str] = []
    for item in candidates:
        value = str(item or "").strip()
        if not value:
            continue
        if value.startswith("file://"):
            value = value[7:]
        path = Path(value)
        if path.is_file():
            resolved_path = str(path.resolve())
            if resolved_path in seen:
                continue
            seen.add(resolved_path)
            resolved.append(resolved_path)
    return resolved


def resolve_event_media_path(event: IncomingEvent) -> str:
    paths = resolve_event_media_paths(event)
    return paths[0] if paths else ""


def build_gateway_attachments_for_event(
    event: IncomingEvent,
    *,
    max_bytes: int = DEFAULT_ATTACHMENT_MAX_BYTES,
) -> list[dict[str, str]]:
    if str(event.message_type or "").strip().lower() != "image":
        return []
    max_attachment_bytes = max(64 * 1024, int(max_bytes))
    attachments: list[dict[str, str]] = []
    for media_path in resolve_event_media_paths(event):
        path = Path(media_path)
        try:
            size_bytes = path.stat().st_size
        except OSError:
            continue
        if size_bytes <= 0 or size_bytes > max_attachment_bytes:
            continue
        mime_type = (mimetypes.guess_type(path.name)[0] or "").strip().lower()
        if not mime_type.startswith("image/"):
            continue
        try:
            content = base64.b64encode(path.read_bytes()).decode("ascii")
        except OSError:
            continue
        attachments.append(
            {
                "type": "image",
                "mimeType": mime_type,
                "fileName": path.name or "image",
                "content": content,
            }
        )
    return attachments


class OpenClawAgentInvoker:
    def __init__(
        self,
        *,
        openclaw_bin: str,
        agent_id: str,
        timeout_sec: float,
        use_local: bool,
        attachment_max_bytes: int = DEFAULT_ATTACHMENT_MAX_BYTES,
    ) -> None:
        self.openclaw_bin = openclaw_bin
        self.agent_id = agent_id
        self.timeout_sec = timeout_sec
        self.use_local = use_local
        self.attachment_max_bytes = max(64 * 1024, int(attachment_max_bytes))

    def _resolve_openclaw_package_root(self) -> Path:
        executable = shutil.which(self.openclaw_bin) or self.openclaw_bin
        resolved = Path(executable).expanduser().resolve()
        return resolved if resolved.is_dir() else resolved.parent

    def _run_gateway_call_cli(
        self,
        *,
        params_json: str,
        call_timeout_ms: int,
        timeout_value: float,
    ) -> subprocess.CompletedProcess[str]:
        cmd = [
            self.openclaw_bin,
            "gateway",
            "call",
            "agent",
            "--expect-final",
            "--json",
            "--timeout",
            str(call_timeout_ms),
            "--params",
            params_json,
        ]
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=max(timeout_value + 40.0, 15.0),
        )

    def _run_gateway_call_stdin(
        self,
        *,
        params: dict[str, Any],
        call_timeout_ms: int,
        timeout_value: float,
    ) -> subprocess.CompletedProcess[str]:
        request_payload = {
            "openclawPackageRoot": str(self._resolve_openclaw_package_root()),
            "method": "agent",
            "params": params,
            "expectFinal": True,
            "timeoutMs": int(call_timeout_ms),
        }
        cmd = [
            "node",
            "--input-type=module",
            "-e",
            GATEWAY_NODE_EVAL,
        ]
        return subprocess.run(
            cmd,
            input=json.dumps(request_payload, ensure_ascii=False),
            capture_output=True,
            text=True,
            timeout=max(timeout_value + 40.0, 15.0),
        )

    def build_session_key(self, event: IncomingEvent, suffix: str = "") -> str:
        _ = suffix
        # Keep customer bucket human-readable:
        # agent:<agent_id>:<platform>:<store_id>:<platform_nickname>
        bucket = str(event.session_key() or "").strip() or "unknown"
        return f"agent:{self.agent_id}:{bucket}"

    def run_prompt(
        self,
        *,
        session_key: str,
        prompt: str,
        attachments: list[dict[str, str]] | None = None,
    ) -> OpenClawReply:
        return self.run_prompt_with_timeout(
            session_key=session_key,
            prompt=prompt,
            timeout_sec=self.timeout_sec,
            attachments=attachments,
        )

    def run_prompt_with_timeout(
        self,
        *,
        session_key: str,
        prompt: str,
        timeout_sec: float,
        attachments: list[dict[str, str]] | None = None,
    ) -> OpenClawReply:
        resolved_session_key = str(session_key or "").strip()
        if not resolved_session_key:
            raise ValueError("missing_session_key")
        timeout_value = max(1.0, float(timeout_sec))
        call_timeout_ms = max(10_000, int(timeout_value * 1000) + 30_000)
        idempotency_key = f"kefu-{uuid4().hex}"
        params = {
            "message": prompt,
            "agentId": self.agent_id,
            "sessionKey": resolved_session_key,
            "idempotencyKey": idempotency_key,
        }
        if attachments:
            params["attachments"] = attachments
        params_json = json.dumps(params, ensure_ascii=False)
        params_json_bytes = len(params_json.encode("utf-8"))
        if attachments or params_json_bytes > MAX_GATEWAY_PARAMS_ARG_BYTES:
            done = self._run_gateway_call_stdin(
                params=params,
                call_timeout_ms=call_timeout_ms,
                timeout_value=timeout_value,
            )
        else:
            done = self._run_gateway_call_cli(
                params_json=params_json,
                call_timeout_ms=call_timeout_ms,
                timeout_value=timeout_value,
            )
        output = f"{done.stdout}\n{done.stderr}".strip()
        if done.returncode != 0:
            raise RuntimeError(f"openclaw_exit_{done.returncode}: {output[:300]}")

        payload = parse_json_output(output)
        reply = extract_reply_text(payload)
        if not reply:
            raise RuntimeError("openclaw_empty_reply")
        return OpenClawReply(text=reply, session_key=resolved_session_key)

    def ask(self, event: IncomingEvent) -> OpenClawReply:
        return self.run_prompt(
            session_key=self.build_session_key(event),
            prompt=build_default_reply_prompt(event),
            attachments=build_gateway_attachments_for_event(
                event,
                max_bytes=self.attachment_max_bytes,
            ),
        )
