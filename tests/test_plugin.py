import json
import os
import random
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from mirrordash_photos import plugin
from mirrordash_photos.plugin import PhotosModule

PREVIEW = Path(plugin.__file__).parent / "preview"


def sample(name):
    return json.loads((PREVIEW / name).read_text())


def make_module(tmp_path, **settings):
    """The module as the mirror sets it up (its folders in tmp_path), with the mirror's helpers faked."""
    config = {"data_dir": str(tmp_path / "data"), "cache_dir": str(tmp_path / "cache"),
              "api_path": "/api/modules/photos-1", **settings}
    os.makedirs(config["data_dir"], exist_ok=True)
    module = PhotosModule(config)
    module.render_template = MagicMock(side_effect=lambda name, **context: context)
    module.translate = lambda key, default=None: default
    module.fetch_json = AsyncMock(return_value=(None, None))
    module.fetch = AsyncMock(return_value=(b"\xff\xd8 a photo \xff\xd9", None))
    return module


def upload(tmp_path, *names):
    folder = tmp_path / "cache" / "files" / "photos"
    folder.mkdir(parents=True, exist_ok=True)
    for i, name in enumerate(names):
        (folder / name).write_bytes(name.encode())
        os.utime(folder / name, (1000 + i, 1000 + i))  # added in the order given
    return folder


def screen(host="127.0.0.1"):
    return SimpleNamespace(client=SimpleNamespace(host=host))


# ── The owner's photos ──

@pytest.mark.asyncio
async def test_uploaded_photos_go_round_in_date_order_and_the_screen_gets_them(tmp_path):
    upload(tmp_path, "2026-07-14_b.jpg", "2026-01-02_a.jpg", "notes.txt")  # what the mirror didn't name isn't shown
    module = make_module(tmp_path)
    shown = [(await module.render())["photo"]["name"] for _ in range(3)]
    assert shown == ["2026-01-02_a.jpg", "2026-07-14_b.jpg", "2026-01-02_a.jpg"]
    url = (await module.render())["photo"]["url"]
    assert url == "/api/modules/photos-1/photo/2026-07-14_b.jpg"
    answer = await module.photo("2026-07-14_b.jpg", screen())
    assert answer.body == b"2026-07-14_b.jpg" and answer.media_type == "image/jpeg"


def test_the_order_by_name_as_added_or_shuffled():
    photos = [{"name": "b.jpg", "added": 1}, {"name": "a.jpg", "added": 2}, {"name": "c.jpg", "added": 3}]
    assert [p["name"] for p in plugin.order(photos, "name", random.Random(1))] == ["a.jpg", "b.jpg", "c.jpg"]
    assert [p["name"] for p in plugin.order(photos, "uploaded", random.Random(1))] == ["b.jpg", "a.jpg", "c.jpg"]
    shuffled = plugin.order(photos, "random", random.Random(1))
    assert sorted(p["name"] for p in shuffled) == ["a.jpg", "b.jpg", "c.jpg"]  # each once


@pytest.mark.asyncio
async def test_only_the_mirrors_screen_gets_a_photo_and_only_one_it_was_given(tmp_path):
    upload(tmp_path, "a.jpg")
    secret = tmp_path / "data" / "state.json"
    module = make_module(tmp_path)
    await module.render()
    for host in ("192.168.1.20", "10.0.0.5"):  # someone on the home network
        with pytest.raises(HTTPException) as refused:
            await module.photo("a.jpg", screen(host))
        assert refused.value.status_code == 403
    for name in ("../data/state.json", "../../etc/passwd", "b.jpg"):  # a name is looked up, never a path
        with pytest.raises(HTTPException) as missing:
            await module.photo(name, screen())
        assert missing.value.status_code == 404
    assert secret.exists()


@pytest.mark.asyncio
async def test_a_restart_goes_on_from_the_photo_it_was_at(tmp_path):
    upload(tmp_path, "a.jpg", "b.jpg", "c.jpg")
    module = make_module(tmp_path)
    await module.render()
    await module.render()
    assert (await make_module(tmp_path).render())["photo"]["name"] == "c.jpg"


@pytest.mark.asyncio
async def test_no_photos_yet_says_where_to_add_them(tmp_path):
    shown = await make_module(tmp_path).render()
    assert shown["photo"] is None and "Add some" in shown["message"]


@pytest.mark.asyncio
async def test_immich_photos_come_through_the_module_with_its_key(tmp_path):
    module = make_module(tmp_path, source="immich", immich_url="http://192.168.1.10:2283/", immich_key="k",
                         immich_album="ab-12")
    module.fetch_json.return_value = ({"assets": [
        {"id": "0002", "type": "IMAGE", "fileCreatedAt": "2026-02-01"},
        {"id": "0003", "type": "VIDEO"},  # a mirror shows no videos
        {"id": "0001", "type": "IMAGE", "fileCreatedAt": "2026-01-01"}]}, None)
    shown = await module.render()
    assert module.fetch_json.call_args.args[0] == "http://192.168.1.10:2283/api/albums/ab-12"
    assert module.fetch_json.call_args.kwargs["headers"]["x-api-key"] == "k"
    assert shown["photo"]["url"] == "/api/modules/photos-1/photo/immich-0001.jpg" and shown["credit"] is None
    assert module.fetch.call_args.args[0] == "http://192.168.1.10:2283/api/assets/0001/thumbnail"
    assert (await module.photo("immich-0001.jpg", screen())).body == b"\xff\xd8 a photo \xff\xd9"
    assert plugin.parse_immich([{"assets": [{"id": "a1"}]}, {"assets": [{"id": "b2"}]}])[1]["id"] == "b2"  # memories


@pytest.mark.asyncio
async def test_immich_without_its_settings_says_what_is_missing(tmp_path):
    shown = await make_module(tmp_path, source="immich", immich_url="http://x", immich_key="k").render()  # no album
    assert "Immich" in shown["message"] and shown["icon"] == "key-round"


# ── The services ──

@pytest.mark.asyncio
async def test_unsplash_photos_come_from_unsplash_with_the_photographer_and_each_is_reported_once(tmp_path):
    module = make_module(tmp_path, source="unsplash", unsplash_key="key", unsplash_query="northern lights",
                         orientation="landscape", interval=30)
    raw = "https://images.unsplash.com/photo-1?ixid=x"
    module.fetch_json.side_effect = lambda url, **kwargs: (
        ([{"urls": {"raw": raw}, "user": {"name": "Alva Berg"}, "links": {"download_location": "https://api.unsplash.com/d/1"}}], None)
        if url.endswith("/random") else ({"url": "x"}, None))
    shown = await module.render()
    assert module.interval == 90  # the demo key's 50 calls an hour
    assert shown["photo"]["url"] == raw + "&w=1920&fit=max&fm=jpg&q=80"
    assert shown["credit"] == "Photo: Alva Berg / Unsplash"
    calls = [(c.args[0], c.kwargs) for c in module.fetch_json.call_args_list]
    assert calls[0][1]["headers"]["Authorization"] == "Client-ID key"
    assert calls[0][1]["params"] == {"count": 10, "content_filter": "high", "query": "northern lights", "orientation": "landscape"}
    assert [url for url, kwargs in calls if url == "https://api.unsplash.com/d/1"] == ["https://api.unsplash.com/d/1"]
    assert calls[1][1]["max_age"] == 0


@pytest.mark.asyncio
async def test_a_service_without_a_key_or_with_a_wrong_one_says_so(tmp_path):
    assert "API key" in (await make_module(tmp_path, source="pexels").render())["message"]
    module = make_module(tmp_path, source="unsplash", unsplash_key="wrong")
    module.fetch_json.return_value = (None, "rejected")
    shown = await module.render()
    assert "rejected" in shown["message"] and shown["photo"] is None


def test_each_services_answer_becomes_photos_with_their_credit():
    pexels = plugin.parse_pexels({"photos": [{"photographer": "Leo Holm", "src": {"original": "https://images.pexels.com/p/1.jpeg"}}]})
    assert pexels == [{"url": "https://images.pexels.com/p/1.jpeg?auto=compress&w=1920", "by": "Leo Holm", "service": "Pexels"}]
    apod = plugin.parse_apod(sample("apod.json"))  # the newest day that's a picture, not a video
    assert apod[0]["by"] == "Sample Photographer" and apod[0]["service"] == "NASA APOD"
    assert plugin.parse_apod([{"date": "2026-11-02", "media_type": "video", "url": "x"}]) == []
    commons = {"image": {"thumbnail": {"source": "https://upload.wikimedia.org/wikipedia/commons/thumb/a/ab/X.jpg/640px-X.jpg"},
                         "artist": {"text": "<a href='//x'>Ann</a> Lee"}, "license": {"type": "CC BY 4.0"}}}
    assert plugin.parse_wikimedia(commons) == [{"url": "https://upload.wikimedia.org/wikipedia/commons/thumb/a/ab/X.jpg/1920px-X.jpg",
                                                "by": "Ann Lee", "service": "Wikimedia Commons, CC BY 4.0"}]
    assert plugin.parse_unsplash({"errors": ["x"]}) == [] and plugin.parse_wikimedia({}) == []


@pytest.mark.asyncio
async def test_the_pictures_of_the_day_ask_for_the_mirrors_day_without_a_key_in_the_url(tmp_path):
    module = make_module(tmp_path, source="apod", globals={"timezone": "Europe/Stockholm"})
    module.fetch_json.return_value = (sample("apod.json"), None)
    shown = await module.render()
    assert shown["credit"] == "Photo: Sample Photographer / NASA APOD"
    url, kwargs = module.fetch_json.call_args.args[0], module.fetch_json.call_args.kwargs
    assert "key" not in url and kwargs["headers"] == {"X-Api-Key": "DEMO_KEY"}
    module = make_module(tmp_path, source="wikimedia")
    module.fetch_json.return_value = (sample("wikimedia.json"), None)
    shown = await module.render()
    assert module.fetch_json.call_args.args[0].startswith("https://api.wikimedia.org/feed/v1/wikipedia/en/featured/")
    assert shown["credit"] == "Photo: Sample Artist / Wikimedia Commons, CC BY-SA 4.0"


@pytest.mark.asyncio
async def test_an_unexpected_answer_keeps_the_card(tmp_path):
    module = make_module(tmp_path, source="pexels", pexels_key="k")
    module.fetch_json.return_value = ({"photos": [{"src": "not a dict"}]}, None)
    shown = await module.render()
    assert shown["photo"] is None and shown["message"]


@pytest.mark.asyncio
async def test_fill_the_screen_is_a_choice_and_the_whole_photo_the_default(tmp_path):
    upload(tmp_path, "2026-01-02_a.jpg")
    assert (await make_module(tmp_path).render())["fill"] is False
    assert (await make_module(tmp_path, fit="cover").render())["fill"] is True
