"""
Telemetry Sanitizer (core/telemetry_sanitizer.py)
Automated privacy and security sanitizer for public repository telemetry export.
- Masks sensitive keys (access_token, api_key, password, secrets, etc.)
- Masks account numbers (e.g. 20201549311 -> LIVE_****49311)
- Strips bulky raw API response payloads
- Scans files/staged diffs prior to Git commit/push to prevent secret leaks
"""

import re
import os
import copy
from typing import Any, Dict, List, Tuple, Union

# Prohibited file extensions & names for Git push
BLOCKED_PATTERNS = [
    re.compile(r"\.env(\..+)?$", re.IGNORECASE),
    re.compile(r".*\.(key|pem|secret|log|db|sqlite|sqlite3)$", re.IGNORECASE),
    re.compile(r"^(token|credential).*", re.IGNORECASE),
    re.compile(r".*(operational_v16\.db|trade_history_v7\.db|experience_memory\.db)$", re.IGNORECASE),
]

# Sensitive keys to redact in dictionary / JSON structures
SENSITIVE_KEY_PATTERNS = [
    re.compile(r"access_token", re.IGNORECASE),
    re.compile(r"refresh_token", re.IGNORECASE),
    re.compile(r"api_key", re.IGNORECASE),
    re.compile(r"appkey", re.IGNORECASE),
    re.compile(r"appsecret", re.IGNORECASE),
    re.compile(r"secret", re.IGNORECASE),
    re.compile(r"password", re.IGNORECASE),
    re.compile(r"authorization", re.IGNORECASE),
    re.compile(r"bearer", re.IGNORECASE),
    re.compile(r"account_password", re.IGNORECASE),
    re.compile(r"full_account_number", re.IGNORECASE),
]

# Regex for strings
RE_BEARER = re.compile(r"Bearer\s+[a-zA-Z0-9_\-\.]+", re.IGNORECASE)
RE_GH_TOKEN = re.compile(r"ghp_[a-zA-Z0-9]{20,}", re.IGNORECASE)
RE_GH_PAT = re.compile(r"github_pat_[a-zA-Z0-9_]{30,}", re.IGNORECASE)

# Account patterns (10 or 11 digits)
RE_LIVE_ACCT = re.compile(r"\b(2020\d{3})(\d{4})\b")
RE_MOCK_ACCT = re.compile(r"\b(5000\d{3})(\d{4})\b")
RE_GENERIC_ACCT = re.compile(r"\b\d{4,8}(\d{4})\b")


class TelemetrySanitizer:
    """Sanitizes objects and validates files before telemetry storage or Git sync."""

    @staticmethod
    def mask_account_number(account_no: Union[str, int, None]) -> str:
        """
        Masks full account numbers.
        Example:
            '20201549311' -> 'LIVE_****49311'
            '50001003032' -> 'MOCK_****0032'
            '12345678' -> 'ACCT_****5678'
        """
        if account_no is None:
            return ""
        s = str(account_no).strip()
        if not s:
            return ""
        if s.upper() in ("LIVE", "MOCK", "UNKNOWN", "PAPER"):
            return s.upper()

        if s.startswith("2020") and len(s) == 11:
            return f"LIVE_****{s[-5:]}"
        elif s.startswith("2020") and len(s) == 10:
            return f"LIVE_****{s[-4:]}"
        elif s.startswith("5000") and len(s) == 11:
            return f"MOCK_****{s[-5:]}"
        elif s.startswith("5000") and len(s) == 10:
            return f"MOCK_****{s[-4:]}"
        elif len(s) >= 8 and s.isdigit():
            last4 = s[-4:]
            return f"ACCT_****{last4}"
        elif len(s) > 4:
            return f"****{s[-4:]}"
        return "****"

    @classmethod
    def sanitize_string(cls, text: str) -> str:
        """Sanitizes text by replacing secrets and sensitive account patterns."""
        if not text:
            return text
        # Redact Authorization / Bearer tokens
        res = RE_BEARER.sub("Bearer [REDACTED]", text)
        res = RE_GH_TOKEN.sub("[REDACTED_GH_TOKEN]", res)
        res = RE_GH_PAT.sub("[REDACTED_GH_PAT]", res)

        # Redact known account patterns in string
        def _replace_live(match):
            return f"LIVE_****{match.group(2)}"

        def _replace_mock(match):
            return f"MOCK_****{match.group(2)}"

        res = RE_LIVE_ACCT.sub(_replace_live, res)
        res = RE_MOCK_ACCT.sub(_replace_mock, res)
        return res

    @classmethod
    def sanitize_data(cls, data: Any) -> Any:
        """
        Recursively sanitizes a dictionary, list, or primitive value.
        Deep-copies to avoid mutating original data structures.
        """
        if isinstance(data, dict):
            clean_dict = {}
            for k, v in data.items():
                k_str = str(k)
                # Check if key is sensitive
                is_sensitive = any(p.search(k_str) for p in SENSITIVE_KEY_PATTERNS)
                if is_sensitive:
                    clean_dict[k] = "[REDACTED]"
                elif k_str in ("account_no", "act_no", "account_number", "account"):
                    clean_dict[k] = cls.mask_account_number(v)
                elif k_str in ("raw_response", "full_response", "response_payload", "api_payload"):
                    clean_dict[k] = "[OMITTED_FOR_TELEMETRY]"
                else:
                    clean_dict[k] = cls.sanitize_data(v)
            return clean_dict
        elif isinstance(data, list):
            return [cls.sanitize_data(item) for item in data]
        elif isinstance(data, tuple):
            return tuple(cls.sanitize_data(item) for item in data)
        elif isinstance(data, str):
            return cls.sanitize_string(data)
        elif isinstance(data, (int, float, bool)) or data is None:
            return data
        else:
            # For other objects, convert to str or dict representation
            try:
                if hasattr(data, "to_dict"):
                    return cls.sanitize_data(data.to_dict())
                return cls.sanitize_string(str(data))
            except Exception:
                return "[UNSERIALIZABLE]"

    @classmethod
    def is_file_blocked(cls, file_path: str) -> bool:
        """Checks if a file path is strictly forbidden from telemetry upload."""
        norm_path = os.path.normpath(file_path).replace("\\", "/")
        base_name = os.path.basename(norm_path)
        for pattern in BLOCKED_PATTERNS:
            if pattern.search(base_name) or pattern.search(norm_path):
                return True
        return False

    @classmethod
    def scan_content_for_leaks(cls, content: str) -> Tuple[bool, List[str]]:
        """
        Scans a text payload or diff for potential unmasked secret leaks.
        Returns: (is_clean: bool, violations: List[str])
        """
        violations = []
        if RE_GH_TOKEN.search(content):
            violations.append("GitHub Personal Access Token detected (ghp_*)")
        if RE_GH_PAT.search(content):
            violations.append("GitHub Fine-grained PAT detected (github_pat_*)")
        if RE_BEARER.search(content) and "[REDACTED]" not in content:
            violations.append("Raw Bearer Token detected")
        if re.search(r"['\"]?(?:access_token|refresh_token|appsecret|password)['\"]?\s*[:=]\s*['\"][^'\"\[]{6,}['\"]", content, re.IGNORECASE):
            violations.append("Raw secret key assignment detected")
        # Check raw 11-digit account numbers (like 20201549311) without mask
        if RE_LIVE_ACCT.search(content):
            violations.append("Raw LIVE account number detected")
        if RE_MOCK_ACCT.search(content):
            violations.append("Raw MOCK account number detected")

        return len(violations) == 0, violations

    @classmethod
    def scan_files_for_push(cls, file_paths: List[str]) -> Tuple[bool, List[str]]:
        """
        Validates an entire list of file paths to be staged/pushed.
        Ensures none are prohibited and their content has no sensitive leaks.
        """
        all_violations = []
        for fp in file_paths:
            if cls.is_file_blocked(fp):
                all_violations.append(f"Blocked file pattern: {fp}")
                continue
            if os.path.isfile(fp):
                try:
                    with open(fp, "r", encoding="utf-8", errors="ignore") as f:
                        text = f.read(500_000)  # Scan up to 500KB per file
                        clean, vios = cls.scan_content_for_leaks(text)
                        if not clean:
                            for v in vios:
                                all_violations.append(f"{fp}: {v}")
                except Exception as err:
                    all_violations.append(f"Cannot read {fp} for leak scan: {err}")

        return len(all_violations) == 0, all_violations
