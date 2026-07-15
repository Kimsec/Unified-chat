"""Third-party Twitch emotes (7TV, BTTV, FFZ) — public APIs, no keys.

Fetched for the broadcaster channel and cached by the runtime; the frontend
does the word-to-image matching, so the message pipeline stays untouched.
"""
from __future__ import annotations

import logging

import aiohttp

log = logging.getLogger("unified_chat.emotes")

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15)


async def fetch_third_party_emotes(twitch_id: str) -> dict[str, str]:
    """Return {emote_name: image_url} for the broadcaster, globals included."""
    async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
        emotes: dict[str, str] = {}

        ffz_global = await _get_json(session, "https://api.frankerfacez.com/v1/set/global")
        if ffz_global:
            emotes.update(_parse_ffz(ffz_global))
        bttv_global = await _get_json(session, "https://api.betterttv.net/3/cached/emotes/global")
        if isinstance(bttv_global, list):
            emotes.update(_parse_bttv(bttv_global))
        stv_global = await _get_json(session, "https://7tv.io/v3/emote-sets/global")
        if stv_global:
            emotes.update(_parse_7tv(stv_global.get("emotes") or []))

        ffz = await _get_json(session, f"https://api.frankerfacez.com/v1/room/id/{twitch_id}")
        if ffz:
            emotes.update(_parse_ffz(ffz))
        bttv = await _get_json(session, f"https://api.betterttv.net/3/cached/users/twitch/{twitch_id}")
        if bttv:
            emotes.update(_parse_bttv((bttv.get("channelEmotes") or []) + (bttv.get("sharedEmotes") or [])))
        stv = await _get_json(session, f"https://7tv.io/v3/users/twitch/{twitch_id}")
        if stv:
            emotes.update(_parse_7tv((stv.get("emote_set") or {}).get("emotes") or []))
        return emotes


async def _get_json(session: aiohttp.ClientSession, url: str):
    try:
        async with session.get(url) as response:
            if response.status >= 400:
                return None
            return await response.json(content_type=None)
    except Exception as exc:
        log.warning("emote fetch failed for %s: %s", url, exc)
        return None


def _parse_ffz(data: dict) -> dict[str, str]:
    emotes: dict[str, str] = {}
    for emote_set in (data.get("sets") or {}).values():
        for emote in emote_set.get("emoticons") or []:
            name = emote.get("name")
            urls = emote.get("urls") or {}
            url = urls.get("2") or urls.get("1") or ""
            if url.startswith("//"):
                url = f"https:{url}"
            if name and url:
                emotes[name] = url
    return emotes


def _parse_bttv(items: list) -> dict[str, str]:
    return {
        item["code"]: f"https://cdn.betterttv.net/emote/{item['id']}/1x"
        for item in items
        if item.get("code") and item.get("id")
    }


def _parse_7tv(items: list) -> dict[str, str]:
    return {
        item["name"]: f"https://cdn.7tv.app/emote/{item['id']}/1x.webp"
        for item in items
        if item.get("name") and item.get("id")
    }
