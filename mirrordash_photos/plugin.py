"""A photo slideshow: photos the owner uploads in the settings, an Immich album, or a photo service on the
internet (Unsplash, Pexels, NASA's or Wikimedia's picture of the day). In a box or, in MirrorDash's
Background position, behind the other modules.

The screen can't reach the home network, so the owner's own photos (uploaded, Immich) reach it through this
module's own address, which answers the mirror's own screen only. A service's photos come straight from the
service: the screen may load pictures from the internet, and Unsplash asks for exactly that."""

import asyncio
import json
import logging
import os
import random
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request, Response  # on the mirror already: not in pyproject's dependencies

logger = logging.getLogger("mirrordash.modules.mirrordash_photos")

SOURCES = ("uploaded", "immich", "unsplash", "pexels", "apod", "wikimedia")
PHOTO_NAME = re.compile(r"^[A-Za-z0-9_-]{1,110}\.jpg$")  # how the mirror names uploaded photos
IMMICH_ID = re.compile(r"^[0-9a-fA-F-]{1,64}$")
SCREEN = ("127.0.0.1", "::1")  # the mirror's own screen reaches the module from here; the home network doesn't
WIDTH = 1920  # the width asked of a photo service. ponytail: a 4K screen gets 1920; upgrade: a Width setting
LIST_AGE = 3600  # seconds an album's or a service's list of photos is kept before it's asked for again
UNSPLASH_BATCH = 10  # photos per call: Unsplash's demo key allows 50 calls an hour, each shown photo is one more
MIN_INTERVAL = {"unsplash": 90}  # 4 batch calls and 40 download calls an hour at most
KEEP_SERVED = 3  # photos the screen may still be fetching: the one shown and the two before it

# What to say when there's no photo: an error -> (translation key, English text, icon)
ERRORS = {
    "rejected": ("key_rejected", "The service rejected the key. Check it in the module's settings.", "key-round"),
    "no_key": ("missing_key", "Add the service's API key in the module's settings.", "key-round"),
    "no_immich": ("missing_immich", "Add Immich's address, an API key and an album in the module's settings.", "key-round"),
    "offline": ("offline", "Can't reach the photos.", "cloud-off"),
}


def order(photos: list[dict], how: str, rng: random.Random) -> list[dict]:
    """The owner's photos in the chosen order: by name (with the date the admin page puts first: the date
    taken), by when they were added, or shuffled (each once before any again)."""
    if how == "random":
        shuffled = list(photos)
        rng.shuffle(shuffled)
        return shuffled
    key = (lambda photo: (photo.get("added", 0), photo["name"])) if how == "uploaded" else (lambda photo: photo["name"])
    return sorted(photos, key=key)


def sized(url: str, width: int = WIDTH) -> str:
    """A service's photo at the width the screen needs (Unsplash and Pexels resize on the fly)."""
    if not url.startswith("https://"):
        return url  # a sample answer's picture in the preview
    joiner = "&" if "?" in url else "?"
    if "unsplash.com" in url:
        return f"{url}{joiner}w={width}&fit=max&fm=jpg&q=80"
    if "pexels.com" in url:
        return f"{url}{joiner}auto=compress&w={width}"
    return url


def wikimedia_sized(url: str, width: int = WIDTH) -> str:
    """A Commons thumbnail at another width: .../thumb/a/ab/Name.jpg/640px-Name.jpg -> 1920px-."""
    return re.sub(r"/(\d+)px-", f"/{width}px-", url) if "/thumb/" in url else url


def parse_unsplash(answer) -> list[dict]:
    photos = answer if isinstance(answer, list) else [answer] if isinstance(answer, dict) else []
    return [{"url": sized(photo["urls"]["raw"]), "by": photo.get("user", {}).get("name", ""), "service": "Unsplash",
             "download": photo.get("links", {}).get("download_location")}
            for photo in photos if isinstance(photo, dict) and photo.get("urls", {}).get("raw")]


def parse_pexels(answer) -> list[dict]:
    photos = answer.get("photos", []) if isinstance(answer, dict) else []
    return [{"url": sized(photo["src"]["original"]), "by": photo.get("photographer", ""), "service": "Pexels"}
            for photo in photos if isinstance(photo, dict) and photo.get("src", {}).get("original")]


def parse_apod(answer) -> list[dict]:
    """The newest picture of the last days (some days it's a video, which a mirror doesn't play)."""
    days = answer if isinstance(answer, list) else [answer] if isinstance(answer, dict) else []
    pictures = [day for day in days if isinstance(day, dict) and day.get("media_type") == "image" and day.get("url")]
    if not pictures:
        return []
    newest = max(pictures, key=lambda day: day.get("date", ""))
    by = " ".join((newest.get("copyright") or "NASA").split())
    return [{"url": newest.get("hdurl") or newest["url"], "by": by, "service": "NASA APOD"}]


def parse_wikimedia(answer) -> list[dict]:
    image = answer.get("image") if isinstance(answer, dict) else None
    if not isinstance(image, dict):
        return []
    url = wikimedia_sized(image.get("thumbnail", {}).get("source", "")) or image.get("image", {}).get("source", "")
    if not url:
        return []
    artist = " ".join(re.sub(r"<[^>]+>", "", image.get("artist", {}).get("text", "")).split())  # sometimes a link
    licence = image.get("license", {}).get("type", "")
    return [{"url": url, "by": artist, "service": f"Wikimedia Commons{', ' + licence if licence else ''}"}]


def parse_immich(answer) -> list[dict]:
    """An album's assets, a random pick, or the memories (each with its assets): the pictures, not videos."""
    if isinstance(answer, dict):
        assets = answer.get("assets", [])
    elif isinstance(answer, list):
        assets = [asset for item in answer if isinstance(item, dict)
                  for asset in (item["assets"] if isinstance(item.get("assets"), list) else [item])]
    else:
        assets = []
    return [{"name": f"immich-{asset['id']}.jpg", "id": asset["id"], "added": asset.get("fileCreatedAt", "")}
            for asset in assets if isinstance(asset, dict) and asset.get("type", "IMAGE") == "IMAGE"
            and IMMICH_ID.match(str(asset.get("id", "")))]


class PhotosModule:
    def __init__(self, config):
        self.config = config
        self.name = "mirrordash_photos"
        self.source = config.get("source") if config.get("source") in SOURCES else "uploaded"
        self.interval = max(int(config.get("interval") or 60), MIN_INTERVAL.get(self.source, 10))
        self.order = config.get("order", "name")
        self.vignette = bool(config.get("vignette", False))
        self.fill = config.get("fit") == "cover"  # fill the box or screen, cropping the edges; else the whole photo
        self.api_path = config.get("api_path", "")
        self.timezone = config.get("globals", {}).get("timezone", "Europe/Stockholm")
        cache = config.get("cache_dir") or ""
        self.uploads = os.path.join(cache, "files", "photos") if cache else ""  # the mirror puts uploads here
        self.state_file = os.path.join(config["data_dir"], "state.json") if config.get("data_dir") else None
        self.position = self.load_state().get(self.source, -1)  # where the slideshow was, across restarts
        self.rng = random.Random()
        self.queue: list[dict] = []  # the owner's photos, in the chosen order
        self.served: dict[str, object] = {}  # name -> a file's path or a photo's bytes, for the screen
        self.router = APIRouter()
        self.router.get("/photo/{name}")(self.photo)

    # ── What the screen gets ──
    async def render(self) -> str:
        message, icon = None, None
        try:
            photo, error = await self.next_photo()
        except Exception as e:  # a service that answered something unexpected: say so, keep the card
            logger.warning(f"{self.source}: {e!r}")
            photo, error = None, "unexpected"
        if photo is None:
            key, text, icon = ERRORS.get(error, ("fetch_failed", "Couldn't get the photos.", "cloud-off")) if error \
                else ("no_photos", "No photos yet. Add some in the module's settings.", "image-plus")
            message = self.translate(key, text)
        credit = None
        if photo and photo.get("service"):  # the services' terms: the photographer, and where it's from
            credit = self.translate("credit", "Photo: {name} / {service}").format(
                name=photo.get("by") or "–", service=photo["service"])
        return self.render_template("widget.html", photo=photo, credit=credit, vignette=self.vignette, fill=self.fill,
                                    message=message, icon=icon)

    async def next_photo(self) -> tuple[dict | None, str | None]:
        """The next photo (with the url the screen loads it from), and an error if the list couldn't be had."""
        photos, error = await getattr(self, f"list_{self.source}")()
        if not photos:
            return None, error
        if self.source in ("uploaded", "immich"):
            names = {photo["name"] for photo in photos}
            if len(self.queue) != len(names) or any(photo["name"] not in names for photo in self.queue):
                self.queue = order(photos, self.order, self.rng)  # photos added or removed
            photos = self.queue
        self.position = (self.position + 1) % len(photos)
        photo = dict(photos[self.position])
        self.save_state()
        if self.source == "immich":
            data, fetch_error = await self.fetch(f"{self.immich_url()}/api/assets/{photo['id']}/thumbnail",
                                                 params={"size": "preview"}, headers=self.immich_headers(),
                                                 max_age=30 * 86400)  # a photo doesn't change: kept a month
            if not data:
                return None, fetch_error or error
            self.serve(photo["name"], data)
        elif self.source == "uploaded":
            self.serve(photo["name"], os.path.join(self.uploads, photo["name"]))
        if "name" in photo:
            photo["url"] = f"{self.api_path}/photo/{photo['name']}"
        if photo.get("download"):  # Unsplash's terms: say that the photo is shown, once
            await self.fetch_json(photo["download"], headers=self.unsplash_headers(), max_age=0)
        return photo, error

    def serve(self, name: str, what: object) -> None:
        self.served.pop(name, None)
        self.served[name] = what
        while len(self.served) > KEEP_SERVED:
            self.served.pop(next(iter(self.served)))

    # ── Your own web address ── the owner's photos, for the mirror's own screen only
    async def photo(self, name: str, request: Request):
        """The screen's <img> asks here. Anyone on the home network could, and these are the family's photos,
        so only the mirror's own screen gets an answer, and only for a photo it was just given (a name never
        becomes a path: it is looked up). ponytail: each module checks this itself; the core could, once a
        second module needs it."""
        if not request.client or request.client.host not in SCREEN:
            raise HTTPException(status_code=403)
        what = self.served.get(name)
        if what is None:
            raise HTTPException(status_code=404)
        if isinstance(what, str):  # an uploaded photo: its path came from the folder's own listing
            try:
                what = await asyncio.to_thread(read_file, what)
            except OSError:
                raise HTTPException(status_code=404)
        return Response(content=what, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})

    # ── The sources ── each gives (photos, error)
    async def list_uploaded(self):
        return await asyncio.to_thread(list_folder, self.uploads), None

    def immich_url(self) -> str:
        return str(self.config.get("immich_url", "")).rstrip("/")

    def immich_headers(self) -> dict:
        return {"x-api-key": str(self.config.get("immich_key", "")), "Accept": "application/json"}

    async def list_immich(self):
        mode, album = self.config.get("immich_mode", "album"), str(self.config.get("immich_album", "")).strip()
        if not self.immich_url() or not self.config.get("immich_key") or (mode == "album" and not IMMICH_ID.match(album)):
            return [], "no_immich"
        if mode == "album":
            reply = await self.fetch_json(f"{self.immich_url()}/api/albums/{album}", headers=self.immich_headers(),
                                          max_age=LIST_AGE)
        elif mode == "memories":
            reply = await self.fetch_json(f"{self.immich_url()}/api/memories", headers=self.immich_headers(),
                                          max_age=LIST_AGE)
        else:
            reply = await self.fetch_json(f"{self.immich_url()}/api/search/random", method="POST",
                                          json={"size": 100, "type": "IMAGE"}, headers=self.immich_headers(),
                                          max_age=LIST_AGE)
        answer, error = reply
        return parse_immich(answer), error

    def unsplash_headers(self) -> dict:
        return {"Authorization": f"Client-ID {self.config.get('unsplash_key', '')}", "Accept-Version": "v1"}

    async def list_unsplash(self):
        if not self.config.get("unsplash_key"):
            return [], "no_key"
        params = {"count": UNSPLASH_BATCH, "content_filter": "high"}
        for setting, param in (("unsplash_query", "query"), ("unsplash_collections", "collections"), ("orientation", "orientation")):
            if (value := str(self.config.get(setting, "")).strip()) and value != "any":
                params[param] = value
        answer, error = await self.fetch_json("https://api.unsplash.com/photos/random", params=params,
                                              headers=self.unsplash_headers(), max_age=self.interval * UNSPLASH_BATCH)
        return parse_unsplash(answer), error

    async def list_pexels(self):
        if not self.config.get("pexels_key"):
            return [], "no_key"
        query = str(self.config.get("pexels_query", "")).strip()
        params = {"per_page": 80, **({"query": query} if query else {})}
        if (orientation := self.config.get("orientation", "any")) != "any" and query:
            params["orientation"] = orientation
        answer, error = await self.fetch_json(f"https://api.pexels.com/v1/{'search' if query else 'curated'}",
                                              params=params, headers={"Authorization": str(self.config["pexels_key"])},
                                              max_age=LIST_AGE)
        return parse_pexels(answer), error

    def today(self) -> date:
        try:
            return datetime.now(ZoneInfo(self.timezone)).date()
        except (KeyError, ValueError):
            return date.today()

    async def list_apod(self):
        # api.nasa.gov takes the key as a header too, so it stays out of the URL; DEMO_KEY allows 30 calls an hour
        start = (self.today() - timedelta(days=6)).isoformat()
        answer, error = await self.fetch_json("https://api.nasa.gov/planetary/apod", params={"start_date": start},
                                              headers={"X-Api-Key": str(self.config.get("nasa_key") or "DEMO_KEY")},
                                              max_age=LIST_AGE)
        return parse_apod(answer), error

    async def list_wikimedia(self):
        answer, error = await self.fetch_json(
            f"https://api.wikimedia.org/feed/v1/wikipedia/en/featured/{self.today():%Y/%m/%d}", max_age=LIST_AGE)
        return parse_wikimedia(answer), error

    # ── Saving files ── where the slideshow was, so a restart (every saved setting) goes on from there
    def load_state(self) -> dict:
        try:
            with open(self.state_file, encoding="utf-8") as f:
                state = json.load(f)
            return state if isinstance(state, dict) else {}
        except (OSError, TypeError, ValueError):
            return {}

    def save_state(self) -> None:
        if self.state_file:  # through a .tmp file: a power cut mid-write leaves the old file whole
            with open(self.state_file + ".tmp", "w", encoding="utf-8") as f:
                json.dump({**self.load_state(), self.source: self.position}, f)
            os.replace(self.state_file + ".tmp", self.state_file)


def list_folder(folder: str) -> list[dict]:
    """The uploaded photos: named as the mirror names them, no links (the folder is the mirror's to fill)."""
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return []
    return [{"name": entry.name, "added": entry.stat(follow_symlinks=False).st_mtime} for entry in entries
            if PHOTO_NAME.match(entry.name) and entry.is_file(follow_symlinks=False)]


def read_file(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read(4 * 1024 * 1024)  # the mirror takes 4 MB at most, and a module's address answers 4 MB at most
