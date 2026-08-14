from __future__ import annotations

import base64
import hashlib
import json
import os
import random
import re
import socket
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any

from Crypto.Cipher import AES


def _now_ms() -> int:
    return int(time.time() * 1000)


def _sha1_hex(parts: list[str]) -> str:
    joined = "".join(sorted([str(x) for x in parts]))
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()


def _xml_to_dict(xml_text: str) -> dict[str, str]:
    root = ET.fromstring(xml_text)
    out: dict[str, str] = {}
    for child in list(root):
        out[str(child.tag)] = str(child.text or "")
    return out


def _build_text_xml(*, to_user: str, from_user: str, create_time: int, content: str) -> str:
    esc_to = ET.Element("x")
    esc_to.text = to_user
    esc_from = ET.Element("x")
    esc_from.text = from_user
    esc_content = ET.Element("x")
    esc_content.text = content
    to_text = esc_to.text or ""
    from_text = esc_from.text or ""
    content_text = esc_content.text or ""
    return (
        "<xml>"
        f"<ToUserName><![CDATA[{to_text}]]></ToUserName>"
        f"<FromUserName><![CDATA[{from_text}]]></FromUserName>"
        f"<CreateTime>{int(create_time)}</CreateTime>"
        "<MsgType><![CDATA[text]]></MsgType>"
        f"<Content><![CDATA[{content_text}]]></Content>"
        "</xml>"
    )


class WeComCrypto:
    def __init__(self, *, token: str, encoding_aes_key: str, corp_id: str) -> None:
        self.token = str(token or "").strip()
        self.encoding_aes_key = str(encoding_aes_key or "").strip()
        self.corp_id = str(corp_id or "").strip()
        if not self.token:
            raise RuntimeError("wecom_token_not_configured")
        if not self.encoding_aes_key:
            raise RuntimeError("wecom_encoding_aes_key_not_configured")
        if not self.corp_id:
            raise RuntimeError("wecom_corp_id_not_configured")

        try:
            self.aes_key = base64.b64decode(self.encoding_aes_key + "=")
        except Exception as exc:
            raise RuntimeError("wecom_encoding_aes_key_invalid_base64") from exc
        if len(self.aes_key) != 32:
            raise RuntimeError("wecom_encoding_aes_key_invalid_length")

    def verify_signature(
        self,
        *,
        msg_signature: str,
        timestamp: str,
        nonce: str,
        encrypted: str,
    ) -> bool:
        expect = _sha1_hex([self.token, timestamp, nonce, encrypted])
        return (msg_signature or "").strip() == expect

    def decrypt_message(self, encrypted: str) -> str:
        cipher = AES.new(self.aes_key, AES.MODE_CBC, self.aes_key[:16])
        try:
            plain_padded = cipher.decrypt(base64.b64decode(encrypted))
        except Exception as exc:
            raise RuntimeError("wecom_decrypt_failed") from exc
        plain = self._pkcs7_unpad(plain_padded)
        if len(plain) < 20:
            raise RuntimeError("wecom_plaintext_too_short")
        msg_len = struct.unpack("!I", plain[16:20])[0]
        if msg_len <= 0 or (20 + msg_len) > len(plain):
            raise RuntimeError("wecom_plaintext_msg_len_invalid")
        xml_bytes = plain[20 : 20 + msg_len]
        receive_id = plain[20 + msg_len :].decode("utf-8", errors="ignore")
        if receive_id != self.corp_id:
            raise RuntimeError("wecom_receive_id_mismatch")
        return xml_bytes.decode("utf-8", errors="ignore")

    def encrypt_message(self, xml_text: str, *, nonce: str, timestamp: str) -> dict[str, str]:
        raw_xml = (xml_text or "").encode("utf-8")
        msg_len = struct.pack("!I", len(raw_xml))
        random16 = os.urandom(16)
        plain = random16 + msg_len + raw_xml + self.corp_id.encode("utf-8")
        padded = self._pkcs7_pad(plain)
        cipher = AES.new(self.aes_key, AES.MODE_CBC, self.aes_key[:16])
        encrypted = base64.b64encode(cipher.encrypt(padded)).decode("utf-8")
        signature = _sha1_hex([self.token, timestamp, nonce, encrypted])
        return {
            "Encrypt": encrypted,
            "MsgSignature": signature,
            "TimeStamp": timestamp,
            "Nonce": nonce,
        }

    def verify_url(self, *, msg_signature: str, timestamp: str, nonce: str, echostr: str) -> str:
        if not self.verify_signature(
            msg_signature=msg_signature,
            timestamp=timestamp,
            nonce=nonce,
            encrypted=echostr,
        ):
            raise RuntimeError("wecom_signature_invalid")
        return self.decrypt_message(echostr)

    def decrypt_callback(
        self,
        *,
        msg_signature: str,
        timestamp: str,
        nonce: str,
        body_xml: str,
    ) -> dict[str, str]:
        payload = _xml_to_dict(body_xml)
        encrypted = str(payload.get("Encrypt") or "").strip()
        if not encrypted:
            raise RuntimeError("wecom_encrypt_empty")
        if not self.verify_signature(
            msg_signature=msg_signature,
            timestamp=timestamp,
            nonce=nonce,
            encrypted=encrypted,
        ):
            raise RuntimeError("wecom_signature_invalid")
        plain_xml = self.decrypt_message(encrypted)
        return _xml_to_dict(plain_xml)

    def build_passive_text_response(
        self,
        *,
        to_user: str,
        from_user: str,
        content: str,
        timestamp: str | None = None,
        nonce: str | None = None,
    ) -> str:
        ts = str(timestamp or int(time.time()))
        seed = nonce or str(random.randint(100000, 999999))
        msg_xml = _build_text_xml(
            to_user=to_user,
            from_user=from_user,
            create_time=int(time.time()),
            content=content,
        )
        encrypted_payload = self.encrypt_message(msg_xml, nonce=seed, timestamp=ts)
        return (
            "<xml>"
            f"<Encrypt><![CDATA[{encrypted_payload['Encrypt']}]]></Encrypt>"
            f"<MsgSignature><![CDATA[{encrypted_payload['MsgSignature']}]]></MsgSignature>"
            f"<TimeStamp>{encrypted_payload['TimeStamp']}</TimeStamp>"
            f"<Nonce><![CDATA[{encrypted_payload['Nonce']}]]></Nonce>"
            "</xml>"
        )

    @staticmethod
    def _pkcs7_pad(raw: bytes, block_size: int = 32) -> bytes:
        pad_len = block_size - (len(raw) % block_size)
        return raw + bytes([pad_len]) * pad_len

    @staticmethod
    def _pkcs7_unpad(raw: bytes, block_size: int = 32) -> bytes:
        if not raw:
            raise RuntimeError("wecom_pkcs7_empty")
        pad_len = raw[-1]
        if pad_len < 1 or pad_len > block_size:
            raise RuntimeError("wecom_pkcs7_invalid_pad")
        return raw[:-pad_len]


@dataclass
class WeComIncomingMessage:
    from_user: str
    to_user: str
    agent_id: str
    msg_type: str
    content: str
    pic_url: str
    msg_id: str
    create_time: str
    event: str
    event_key: str
    raw: dict[str, str]

    def dedup_key(self) -> str:
        if self.msg_id:
            return f"msgid:{self.msg_id}"
        stable = "|".join(
            [
                self.from_user,
                self.to_user,
                self.agent_id,
                self.msg_type,
                self.event,
                self.event_key,
                self.create_time,
                self.content,
                self.pic_url,
            ]
        )
        return "stable:" + hashlib.sha1(stable.encode("utf-8")).hexdigest()[:32]


class WeComMessageDedup:
    def __init__(self, *, ttl_sec: int = 600, max_items: int = 10000) -> None:
        self.ttl_ms = max(30, int(ttl_sec)) * 1000
        self.max_items = max(200, int(max_items))
        self._lock = threading.Lock()
        self._seen: dict[str, int] = {}

    def mark_once(self, key: str) -> bool:
        now = _now_ms()
        with self._lock:
            self._cleanup(now)
            exp = self._seen.get(key)
            if exp and exp > now:
                return False
            self._seen[key] = now + self.ttl_ms
            return True

    def _cleanup(self, now: int) -> None:
        if len(self._seen) > self.max_items:
            keys = sorted(self._seen.items(), key=lambda kv: kv[1])
            for k, _ in keys[: len(self._seen) - self.max_items]:
                self._seen.pop(k, None)
        expired = [k for k, exp in self._seen.items() if exp <= now]
        for k in expired:
            self._seen.pop(k, None)


class WeComAppClient:
    def __init__(
        self,
        *,
        corp_id: str,
        app_secret: str,
        agent_id: str,
        timeout_sec: float,
    ) -> None:
        self.corp_id = str(corp_id or "").strip()
        self.app_secret = str(app_secret or "").strip()
        self.agent_id = str(agent_id or "").strip()
        self.timeout_sec = max(3.0, float(timeout_sec))
        self._lock = threading.Lock()
        self._access_token = ""
        self._token_expire_at_ms = 0

    def send_text(self, *, to_user: str, content: str) -> dict[str, Any]:
        payload = {
            "touser": str(to_user or "").strip(),
            "msgtype": "text",
            "agentid": self._agent_id_value(),
            "text": {"content": str(content or "").strip()},
            "safe": 0,
            "enable_duplicate_check": 0,
            "enable_id_trans": 0,
        }
        data = self._post_json(
            url=f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={self._get_access_token()}",
            payload=payload,
        )
        if int(data.get("errcode", -1)) != 0:
            return {"ok": False, "message": "wecom_send_failed", "data": data}
        return {"ok": True, "message": "ok", "data": data}

    def send_image_media_id(self, *, to_user: str, media_id: str) -> dict[str, Any]:
        payload = {
            "touser": str(to_user or "").strip(),
            "msgtype": "image",
            "agentid": self._agent_id_value(),
            "image": {"media_id": str(media_id or "").strip()},
            "safe": 0,
            "enable_duplicate_check": 0,
            "enable_id_trans": 0,
        }
        data = self._post_json(
            url=f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={self._get_access_token()}",
            payload=payload,
        )
        if int(data.get("errcode", -1)) != 0:
            return {"ok": False, "message": "wecom_send_image_failed", "data": data}
        return {"ok": True, "message": "ok", "data": data}

    def upload_image_bytes(
        self,
        *,
        image_bytes: bytes,
        filename: str = "image.jpg",
        content_type: str = "image/jpeg",
    ) -> dict[str, Any]:
        if not image_bytes:
            return {"ok": False, "message": "wecom_image_empty", "data": {}}

        token = self._get_access_token()
        url = f"https://qyapi.weixin.qq.com/cgi-bin/media/upload?access_token={token}&type=image"
        data = self._post_multipart_media(
            url=url,
            field_name="media",
            filename=filename,
            content_type=content_type,
            payload=image_bytes,
        )
        if int(data.get("errcode", -1)) != 0:
            return {"ok": False, "message": "wecom_upload_image_failed", "data": data}
        media_id = str(data.get("media_id") or "").strip()
        if not media_id:
            return {"ok": False, "message": "wecom_upload_media_id_empty", "data": data}
        return {"ok": True, "message": "ok", "data": data}

    def _agent_id_value(self) -> int | str:
        try:
            return int(self.agent_id)
        except Exception:
            return self.agent_id

    def _get_access_token(self) -> str:
        now = _now_ms()
        with self._lock:
            if self._access_token and now < self._token_expire_at_ms:
                return self._access_token
            if not self.corp_id or not self.app_secret:
                raise RuntimeError("wecom_corpid_or_secret_not_configured")
            params = urllib.parse.urlencode(
                {"corpid": self.corp_id, "corpsecret": self.app_secret}
            )
            url = f"https://qyapi.weixin.qq.com/cgi-bin/gettoken?{params}"
            data = self._get_json(url)
            if int(data.get("errcode", -1)) != 0:
                raise RuntimeError(f"wecom_gettoken_failed:{json.dumps(data, ensure_ascii=False)}")
            token = str(data.get("access_token") or "").strip()
            if not token:
                raise RuntimeError("wecom_access_token_empty")
            expires_in = int(data.get("expires_in") or 7200)
            self._access_token = token
            self._token_expire_at_ms = now + max(60, expires_in - 120) * 1000
            return token

    def _get_json(self, url: str) -> dict[str, Any]:
        req = urllib.request.Request(url=url, method="GET")
        req.add_header("Accept", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_sec) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, socket.timeout):
                raise TimeoutError("wecom_get_timeout") from exc
            raise
        except TimeoutError:
            raise
        data = json.loads(raw or "{}")
        return data if isinstance(data, dict) else {}

    def _post_json(self, *, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url=url, data=body, method="POST")
        req.add_header("Content-Type", "application/json; charset=utf-8")
        req.add_header("Accept", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_sec) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, socket.timeout):
                raise TimeoutError("wecom_post_timeout") from exc
            raise
        except TimeoutError:
            raise
        data = json.loads(raw or "{}")
        return data if isinstance(data, dict) else {}

    def _post_multipart_media(
        self,
        *,
        url: str,
        field_name: str,
        filename: str,
        content_type: str,
        payload: bytes,
    ) -> dict[str, Any]:
        boundary = f"----WeComFormBoundary{random.randint(100000, 999999)}"
        head = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode("utf-8")
        tail = f"\r\n--{boundary}--\r\n".encode("utf-8")
        body = head + payload + tail

        req = urllib.request.Request(url=url, data=body, method="POST")
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
        req.add_header("Accept", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_sec) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, socket.timeout):
                raise TimeoutError("wecom_multipart_post_timeout") from exc
            raise
        except TimeoutError:
            raise
        data = json.loads(raw or "{}")
        return data if isinstance(data, dict) else {}


class WeComBridge:
    def __init__(
        self,
        *,
        token: str,
        encoding_aes_key: str,
        corp_id: str,
        app_secret: str,
        agent_id: str,
        timeout_sec: float,
    ) -> None:
        self.crypto = WeComCrypto(
            token=token,
            encoding_aes_key=encoding_aes_key,
            corp_id=corp_id,
        )
        self.app_client = WeComAppClient(
            corp_id=corp_id,
            app_secret=app_secret,
            agent_id=agent_id,
            timeout_sec=timeout_sec,
        )
        self.agent_id = str(agent_id or "").strip()
        self.dedup = WeComMessageDedup(ttl_sec=600, max_items=10000)

    def verify_url(self, *, msg_signature: str, timestamp: str, nonce: str, echostr: str) -> str:
        return self.crypto.verify_url(
            msg_signature=msg_signature,
            timestamp=timestamp,
            nonce=nonce,
            echostr=echostr,
        )

    def parse_callback_message(
        self,
        *,
        msg_signature: str,
        timestamp: str,
        nonce: str,
        body_xml: str,
    ) -> WeComIncomingMessage:
        plain = self.crypto.decrypt_callback(
            msg_signature=msg_signature,
            timestamp=timestamp,
            nonce=nonce,
            body_xml=body_xml,
        )
        msg = WeComIncomingMessage(
            from_user=str(plain.get("FromUserName") or "").strip(),
            to_user=str(plain.get("ToUserName") or "").strip(),
            agent_id=str(plain.get("AgentID") or "").strip(),
            msg_type=str(plain.get("MsgType") or "").strip().lower(),
            content=str(plain.get("Content") or "").strip(),
            pic_url=str(plain.get("PicUrl") or "").strip(),
            msg_id=str(plain.get("MsgId") or "").strip(),
            create_time=str(plain.get("CreateTime") or "").strip(),
            event=str(plain.get("Event") or "").strip().lower(),
            event_key=str(plain.get("EventKey") or "").strip(),
            raw=plain,
        )
        return msg

    def should_process(self, msg: WeComIncomingMessage) -> bool:
        if msg.agent_id and self.agent_id and msg.agent_id != self.agent_id:
            return False
        if msg.msg_type in {"text", "image"}:
            return bool(msg.from_user)
        # only react to enter_agent with a welcome when configured by caller
        if msg.msg_type == "event" and msg.event == "enter_agent":
            return True
        return False

    def mark_once(self, msg: WeComIncomingMessage) -> bool:
        return self.dedup.mark_once(msg.dedup_key())

    def send_text(self, *, to_user: str, content: str) -> dict[str, Any]:
        return self.app_client.send_text(to_user=to_user, content=content)

    def send_image_url(self, *, to_user: str, image_url: str) -> dict[str, Any]:
        url = str(image_url or "").strip()
        if not url:
            return {"ok": False, "message": "wecom_image_url_empty", "data": {}}
        if not re.match(r"^https?://", url, flags=re.IGNORECASE):
            return {"ok": False, "message": "wecom_image_url_invalid", "data": {"url": url}}

        try:
            image_bytes, content_type = self._download_image(url)
        except Exception as exc:
            return {
                "ok": False,
                "message": f"wecom_image_download_failed:{type(exc).__name__}",
                "data": {"url": url},
            }

        filename = self._guess_filename(url, content_type)
        uploaded = self.app_client.upload_image_bytes(
            image_bytes=image_bytes,
            filename=filename,
            content_type=content_type,
        )
        if not uploaded.get("ok"):
            return uploaded
        media_id = str(((uploaded.get("data") or {}).get("media_id") or "")).strip()
        if not media_id:
            return {"ok": False, "message": "wecom_upload_media_id_empty", "data": uploaded.get("data") or {}}
        return self.app_client.send_image_media_id(to_user=to_user, media_id=media_id)

    def _download_image(self, url: str) -> tuple[bytes, str]:
        req = urllib.request.Request(url=url, method="GET")
        req.add_header("User-Agent", "Mozilla/5.0")
        with urllib.request.urlopen(req, timeout=self.app_client.timeout_sec) as resp:
            content_type = str(resp.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            payload = resp.read()
        if not payload:
            raise RuntimeError("empty_image_payload")
        max_size = 2 * 1024 * 1024
        if len(payload) > max_size:
            raise RuntimeError("image_too_large")
        if not content_type.startswith("image/"):
            # Still allow when type header missing; block obvious non-image types.
            if content_type and content_type not in {"application/octet-stream"}:
                raise RuntimeError("content_type_not_image")
            content_type = "image/jpeg"
        return payload, content_type

    def _guess_filename(self, url: str, content_type: str) -> str:
        parsed = urllib.parse.urlparse(url)
        leaf = os.path.basename(parsed.path or "").strip()
        if leaf and "." in leaf:
            return leaf[:120]
        ext = ".jpg"
        if content_type == "image/png":
            ext = ".png"
        elif content_type == "image/gif":
            ext = ".gif"
        elif content_type == "image/webp":
            ext = ".webp"
        return f"image{ext}"
