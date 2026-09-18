# -*- coding: utf-8 -*-
"""[FINAL MASTER v16.0] Position Override Store (core/position_override_store.py)
Stores user-verified actual purchase prices (평단가) for broker holdings.
Allows overriding broker API defaults with real MTS purchase prices.
"""

import os
import json
import logging
from typing import Dict, Any, Optional

logger = logging.getLogger("PositionOverrideStore")

OVERRIDE_FILE = "data/position_overrides.json"


class PositionOverrideStore:
    _instance = None

    def __init__(self, filepath: str = OVERRIDE_FILE):
        self.filepath = filepath
        self._ensure_file()

    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            cls._instance = PositionOverrideStore()
        return cls._instance

    def _ensure_file(self):
        dirname = os.path.dirname(self.filepath)
        if dirname:
            os.makedirs(dirname, exist_ok=True)
        if not os.path.exists(self.filepath):
            with open(self.filepath, "w", encoding="utf-8") as f:
                json.dump({"live": {}, "mock": {}}, f, indent=2, ensure_ascii=False)

    def load_overrides(self) -> Dict[str, Dict[str, Any]]:
        try:
            with open(self.filepath, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Failed to load position overrides: {e}")
            return {"live": {}, "mock": {}}

    def get_override(self, mode: str, symbol: str) -> Optional[float]:
        data = self.load_overrides()
        mode_key = mode.lower()
        entry = data.get(mode_key, {}).get(symbol)
        if entry is not None:
            if isinstance(entry, (int, float)):
                return float(entry)
            if isinstance(entry, dict) and "entry_price" in entry:
                return float(entry["entry_price"])
        return None

    def set_override(self, mode: str, symbol: str, entry_price: float, note: str = ""):
        data = self.load_overrides()
        mode_key = mode.lower()
        if mode_key not in data:
            data[mode_key] = {}
        data[mode_key][symbol] = {
            "entry_price": float(entry_price),
            "note": note
        }
        with open(self.filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        logger.info(f"Updated position override for [{mode_key}] {symbol} -> {entry_price:,.0f}원")
