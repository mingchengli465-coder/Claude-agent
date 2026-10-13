"""What San Francisco and Singapore look like right now, for the owner's /live: a still from a public
camera, a minute or two old. Both publish their traffic cameras' latest picture, free and without a key:

- San Francisco: Caltrans District 4 (the Bay Area) lists its cameras, each with its latest JPEG.
  The Bay Bridge ones look across the water towards the city.
- Singapore: the Land Transport Authority's traffic cameras, through data.gov.sg.

    snapshot(key, session=None) -> Shot(jpeg, caption)    raises LiveCamError
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from aiohttp import ClientSession, ClientTimeout

CALTRANS_LIST = os.environ.get("LIVECAM_CALTRANS_URL", "https://cwwp2.dot.ca.gov/data/d4/cctv/cctvStatusD04.json")
SG_LIST = os.environ.get("LIVECAM_SG_URL", "https://api.data.gov.sg/v1/transport/traffic-images")
# the Bay Bridge and the islands in the middle of it: the views over the bay to the city
SF_VIEW = re.compile(r"bay bridge|sfobb|treasure island|yerba buena|\bybi\b", re.I)
# Singapore's cameras nearest Marina Bay first (Marina Coastal Expressway, then East Coast Parkway)
SG_PREFERRED = ("1501", "1502", "1503", "1504", "1505", "1001", "1002", "1003")
MAX_BYTES = 5_000_000
AGENT = "vinc-livecam/1.0 (+https://mingchengli465-coder.github.io/Claude-agent/)"
HOME_TZ = "Asia/Shanghai"


class LiveCamError(RuntimeError):
    pass


@dataclass(frozen=True)
class Place:
    name: str
    tz: str
    source: str


@dataclass
class Shot:
    jpeg: bytes
    caption: str


PLACES = {
    "sf": Place("旧金山 · 海湾大桥", "America/Los_Angeles", "Caltrans 公开路况摄像头"),
    "sg": Place("新加坡", "Asia/Singapore", "新加坡陆路交通管理局公开路况摄像头"),
}
ALIASES = {"sf": "sf", "旧金山": "sf", "三藩市": "sf", "sanfrancisco": "sf", "sg": "sg", "新加坡": "sg",
           "singapore": "sg"}


def _first(node, key: str):
    """The first value of `key` anywhere under `node`."""
    if isinstance(node, dict):
        if key in node:
            return node[key]
        node = list(node.values())
    if isinstance(node, list):
        for value in node:
            found = _first(value, key)
            if found:
                return found
    return None


def caltrans_cameras(data) -> list[tuple[str, str]]:
    """(name, latest picture) for every camera in service in Caltrans' list."""
    found: list[tuple[str, str]] = []

    def walk(node):
        if isinstance(node, dict):
            location, image = node.get("location"), node.get("imageData")
            if isinstance(location, dict) and isinstance(image, dict):
                url = _first(image, "currentImageURL")
                name = " · ".join(str(location[k]) for k in ("locationName", "nearbyPlace") if location.get(k))
                if url and str(node.get("inService", "true")).lower() != "false":
                    found.append((name, str(url)))
                return
            node = list(node.values())
        if isinstance(node, list):
            for value in node:
                walk(value)

    walk(data)
    return found


def pick_sf(data) -> tuple[str, str]:
    cameras = caltrans_cameras(data)
    views = [c for c in cameras if SF_VIEW.search(c[0])]
    if not views:
        raise LiveCamError("Caltrans 的列表里没找到海湾大桥的摄像头")
    return views[0]


def pick_sg(data) -> tuple[str, str]:
    items = data.get("items") if isinstance(data, dict) else None
    cameras = [c for c in ((items or [{}])[0].get("cameras") or []) if isinstance(c, dict) and c.get("image")]
    if not cameras:
        raise LiveCamError("data.gov.sg 现在没有路况摄像头的照片")
    by_id = {str(c.get("camera_id")): c for c in cameras}
    camera = next((by_id[i] for i in SG_PREFERRED if i in by_id), cameras[0])
    return f"摄像头 {camera.get('camera_id')}", str(camera["image"])


def caption(key: str, camera: str, now: dt.datetime | None = None) -> str:
    place = PLACES[key]
    now = now or dt.datetime.now(dt.timezone.utc)
    there, home = now.astimezone(ZoneInfo(place.tz)), now.astimezone(ZoneInfo(HOME_TZ))
    dark = "（那边现在是夜里，画面会比较暗）" if there.hour >= 19 or there.hour < 6 else ""
    return (f"📷 {place.name}，当地时间 {there:%H:%M}{dark}\n北京时间 {home:%H:%M}\n"
            f"{camera} · {place.source}，一两分钟前拍的")


async def _get(session: ClientSession, url: str) -> bytes:
    async with session.get(url, headers={"User-Agent": AGENT}) as r:
        if r.status != 200:
            raise LiveCamError(f"{url.split('/')[2]} 返回 {r.status}")
        body = await r.content.read(MAX_BYTES + 1)
    if len(body) > MAX_BYTES:
        raise LiveCamError("照片太大")
    return body


async def snapshot(key: str, session: ClientSession | None = None, now: dt.datetime | None = None) -> Shot:
    """The latest picture from the place's camera, with where and when it is."""
    if key not in PLACES:
        raise LiveCamError(f"没有这个地方：{key}")
    own = session is None
    session = session or ClientSession(timeout=ClientTimeout(total=40))
    try:
        listing, pick = (CALTRANS_LIST, pick_sf) if key == "sf" else (SG_LIST, pick_sg)
        try:
            data = json.loads(await _get(session, listing))
        except ValueError:
            raise LiveCamError("摄像头列表看不懂") from None
        camera, url = pick(data)
        jpeg = await _get(session, url)
        if not jpeg.startswith(b"\xff\xd8"):
            raise LiveCamError("拿到的不是照片")
        return Shot(jpeg, caption(key, camera, now))
    except LiveCamError:
        raise
    except Exception as exc:  # noqa: BLE001 - the network, a timeout: one readable line
        raise LiveCamError(f"连不上摄像头：{exc.__class__.__name__}") from exc
    finally:
        if own:
            await session.close()
