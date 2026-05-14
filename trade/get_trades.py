import json
import os
import time
from pathlib import Path
from typing import Any

import requests
from base58 import b58decode
from base64 import urlsafe_b64encode
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from dotenv import load_dotenv


load_dotenv()


BASE_URL = os.getenv("ORDERLY_BASE_URL", "https://api.orderly.org")
ORDERLY_ACCOUNT_ID = os.getenv("ORDERLY_ACCOUNT_ID")
ORDERLY_SECRET = os.getenv("ORDERLY_SECRET")
ORDERLY_PUBLIC_KEY = os.getenv("ORDERLY_PUBLIC_KEY")


def _build_private_key(secret: str) -> Ed25519PrivateKey:
	normalized = secret.replace("ed25519:", "") if secret.startswith("ed25519:") else secret
	return Ed25519PrivateKey.from_private_bytes(b58decode(normalized))


def _build_signed_headers(path_with_query: str) -> dict[str, str]:
	if not ORDERLY_ACCOUNT_ID or not ORDERLY_SECRET or not ORDERLY_PUBLIC_KEY:
		raise ValueError(
			"Missing required env vars: ORDERLY_ACCOUNT_ID, ORDERLY_SECRET, ORDERLY_PUBLIC_KEY"
		)

	timestamp = str(int(time.time() * 1000))
	private_key = _build_private_key(ORDERLY_SECRET)
	message = f"{timestamp}GET{path_with_query}"
	signature = urlsafe_b64encode(private_key.sign(message.encode())).decode()

	return {
		"Content-Type": "application/x-www-form-urlencoded",
		"orderly-timestamp": timestamp,
		"orderly-account-id": ORDERLY_ACCOUNT_ID,
		"orderly-key": ORDERLY_PUBLIC_KEY,
		"orderly-signature": signature,
	}


def _extract_trade_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
	data = payload.get("data")

	if isinstance(data, list):
		return data

	if isinstance(data, dict):
		for key in ("rows", "items", "trades"):
			value = data.get(key)
			if isinstance(value, list):
				return value

	return []


def get_trades(symbol_filter: str = "NEAR_USDC") -> list[dict[str, Any]]:
	"""Fetch private trades from Orderly, filter by symbol, and export to data/all_trades.json."""
	path = "/v1/trades"
	headers = _build_signed_headers(path)
	url = f"{BASE_URL}{path}"

	response = requests.get(url, headers=headers, timeout=15)
	response.raise_for_status()

	payload = response.json()
	trades = _extract_trade_rows(payload)
	filtered = [
		trade
		for trade in trades
		if symbol_filter in str(trade.get("symbol", ""))
	]

	project_root = Path(__file__).resolve().parents[1]
	output_path = project_root / "data" / "all_trades.json"
	output_path.parent.mkdir(parents=True, exist_ok=True)

	with output_path.open("w", encoding="utf-8") as fp:
		json.dump(filtered, fp, ensure_ascii=False, indent=2)

	return filtered


if __name__ == "__main__":
	near_trades = get_trades(symbol_filter="NEAR_USDC")
	print(f"Exported {len(near_trades)} trades to data/all_trades.json")

