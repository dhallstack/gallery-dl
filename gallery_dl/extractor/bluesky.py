# -*- coding: utf-8 -*-

# Copyright 2024 Mike Fährmann
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 as
# published by the Free Software Foundation.

"""Extractors for https://bsky.app/"""

from .common import Extractor, Message
from .. import text, util, exception
from ..cache import cache, memcache

BASE_PATTERN = r"(?:https?://)?bsky\.app"
USER_PATTERN = BASE_PATTERN + r"/profile/([^/?#]+)"


class BlueskyExtractor(Extractor):
    """Base class for bluesky extractors"""
    category = "bluesky"
    directory_fmt = ("{category}", "{author[handle]}")
    filename_fmt = "{createdAt[:19]}_{post_id}_{num}.{extension}"
    archive_fmt = "{filename}"
    root = "https://bsky.app"

    def __init__(self, match):
        Extractor.__init__(self, match)
        self.user = match.group(1)

    def _init(self):
        self.api = BlueskyAPI(self)

    def items(self):
        for post in self.posts():
            post = post["post"]
            post.update(post["record"])
            del post["record"]

            images = ()
            if "embed" in post:
                media = post["embed"]
                if "media" in media:
                    media = media["media"]
                if "images" in media:
                    images = media["images"]

            if "facets" in post:
                post["hashtags"] = tags = []
                post["mentions"] = dids = []
                post["uris"] = uris = []
                for facet in post["facets"]:
                    features = facet["features"][0]
                    if "tag" in features:
                        tags.append(features["tag"])
                    elif "did" in features:
                        dids.append(features["did"])
                    elif "uri" in features:
                        uris.append(features["uri"])
            else:
                post["hashtags"] = post["mentions"] = post["uris"] = ()

            post["post_id"] = post["uri"].rpartition("/")[2]
            post["count"] = len(images)
            post["date"] = text.parse_datetime(
                post["createdAt"][:19], "%Y-%m-%dT%H:%M:%S")

            yield Message.Directory, post

            if not images:
                continue

            base = ("https://bsky.social/xrpc/com.atproto.sync.getBlob"
                    "?did={}&cid=".format(post["author"]["did"]))
            post["num"] = 0

            for file in images:
                post["num"] += 1
                post["description"] = file["alt"]

                try:
                    aspect = file["aspectRatio"]
                    post["width"] = aspect["width"]
                    post["height"] = aspect["height"]
                except KeyError:
                    post["width"] = post["height"] = 0

                image = file["image"]
                post["filename"] = link = image["ref"]["$link"]
                post["extension"] = image["mimeType"].rpartition("/")[2]

                yield Message.Url, base + link, post

    def posts(self):
        return ()


class BlueskyUserExtractor(BlueskyExtractor):
    subcategory = "user"
    pattern = USER_PATTERN + r"$"
    example = "https://bsky.app/profile/HANDLE"

    def initialize(self):
        pass

    def items(self):
        base = "{}/profile/{}/".format(self.root, self.user)
        return self._dispatch_extractors((
            (BlueskyPostsExtractor  , base + "posts"),
            (BlueskyRepliesExtractor, base + "replies"),
            (BlueskyMediaExtractor  , base + "media"),
            (BlueskyLikesExtractor  , base + "likes"),
        ), ("media",))


class BlueskyPostsExtractor(BlueskyExtractor):
    subcategory = "posts"
    pattern = USER_PATTERN + r"/posts"
    example = "https://bsky.app/profile/HANDLE/posts"

    def posts(self):
        return self.api.get_author_feed(self.user, "posts_and_author_threads")


class BlueskyRepliesExtractor(BlueskyExtractor):
    subcategory = "replies"
    pattern = USER_PATTERN + r"/replies"
    example = "https://bsky.app/profile/HANDLE/replies"

    def posts(self):
        return self.api.get_author_feed(self.user, "posts_with_replies")


class BlueskyMediaExtractor(BlueskyExtractor):
    subcategory = "media"
    pattern = USER_PATTERN + r"/media"
    example = "https://bsky.app/profile/HANDLE/media"

    def posts(self):
        return self.api.get_author_feed(self.user, "posts_with_media")


class BlueskyLikesExtractor(BlueskyExtractor):
    subcategory = "likes"
    pattern = USER_PATTERN + r"/likes"
    example = "https://bsky.app/profile/HANDLE/likes"

    def posts(self):
        return self.api.get_actor_likes(self.user)


class BlueskyFeedExtractor(BlueskyExtractor):
    subcategory = "feed"
    pattern = USER_PATTERN + r"/feed/([^/?#]+)"
    example = "https://bsky.app/profile/HANDLE/feed/NAME"

    def __init__(self, match):
        BlueskyExtractor.__init__(self, match)
        self.feed = match.group(2)

    def posts(self):
        return self.api.get_feed(self.user, self.feed)


class BlueskyListExtractor(BlueskyExtractor):
    subcategory = "list"
    pattern = USER_PATTERN + r"/lists/([^/?#]+)"
    example = "https://bsky.app/profile/HANDLE/lists/ID"

    def __init__(self, match):
        BlueskyExtractor.__init__(self, match)
        self.list = match.group(2)

    def posts(self):
        return self.api.get_list_feed(self.user, self.list)


class BlueskyFollowingExtractor(BlueskyExtractor):
    subcategory = "following"
    pattern = USER_PATTERN + r"/follows"
    example = "https://bsky.app/profile/HANDLE/follows"

    def items(self):
        for user in self.api.get_follows(self.user):
            url = "https://bsky.app/profile/" + user["did"]
            yield Message.Queue, url, user


class BlueskyPostExtractor(BlueskyExtractor):
    subcategory = "post"
    pattern = USER_PATTERN + r"/post/([^/?#]+)"
    example = "https://bsky.app/profile/HANDLE/post/ID"

    def __init__(self, match):
        BlueskyExtractor.__init__(self, match)
        self.post_id = match.group(2)

    def posts(self):
        return self.api.get_post_thread(self.user, self.post_id)


class BlueskyAPI():
    """Interface for the Bluesky API

    https://www.docs.bsky.app/docs/category/http-reference
    """

    def __init__(self, extractor):
        self.headers = {}
        self.extractor = extractor
        self.log = extractor.log

        self.username, self.password = extractor._get_auth_info()
        if self.username:
            self.root = "https://bsky.social"
        else:
            self.root = "https://api.bsky.app"
            self.authenticate = util.noop

    def get_actor_likes(self, actor):
        endpoint = "app.bsky.feed.getActorLikes"
        params = {
            "actor": self._did_from_actor(actor),
            "limit": "100",
        }
        return self._pagination(endpoint, params)

    def get_author_feed(self, actor, filter="posts_and_author_threads"):
        endpoint = "app.bsky.feed.getAuthorFeed"
        params = {
            "actor" : self._did_from_actor(actor),
            "filter": filter,
            "limit" : "100",
        }
        return self._pagination(endpoint, params)

    def get_feed(self, actor, feed):
        endpoint = "app.bsky.feed.getFeed"
        params = {
            "feed" : "at://{}/app.bsky.feed.generator/{}".format(
                self._did_from_actor(actor), feed),
            "limit": "100",
        }
        return self._pagination(endpoint, params)

    def get_follows(self, actor):
        endpoint = "app.bsky.graph.getFollows"
        params = {
            "actor": self._did_from_actor(actor),
            "limit": "100",
        }
        return self._pagination(endpoint, params, "follows")

    def get_list_feed(self, actor, list):
        endpoint = "app.bsky.feed.getListFeed"
        params = {
            "list" : "at://{}/app.bsky.graph.list/{}".format(
                self._did_from_actor(actor), list),
            "limit": "100",
        }
        return self._pagination(endpoint, params)

    def get_post_thread(self, actor, post_id):
        endpoint = "app.bsky.feed.getPostThread"
        params = {
            "uri": "at://{}/app.bsky.feed.post/{}".format(
                self._did_from_actor(actor), post_id),
        }
        return (self._call(endpoint, params)["thread"],)

    def get_profile(self, actor):
        endpoint = "app.bsky.actor.getProfile"
        params = {"actor": self._did_from_actor(actor)}
        return self._call(endpoint, params)

    @memcache(keyarg=1)
    def resolve_handle(self, handle):
        endpoint = "com.atproto.identity.resolveHandle"
        params = {"handle": handle}
        return self._call(endpoint, params)["did"]

    def _did_from_actor(self, actor):
        if actor.startswith("did:"):
            return actor
        return self.resolve_handle(actor)

    def authenticate(self):
        self.headers["Authorization"] = self._authenticate_impl(self.username)

    @cache(maxage=3600, keyarg=1)
    def _authenticate_impl(self, username):
        refresh_token = _refresh_token_cache(username)

        if refresh_token:
            self.log.info("Refreshing access token for %s", username)
            endpoint = "com.atproto.server.refreshSession"
            headers = {"Authorization": "Bearer " + refresh_token}
            data = None
        else:
            self.log.info("Logging in as %s", username)
            endpoint = "com.atproto.server.createSession"
            headers = None
            data = {
                "identifier": username,
                "password"  : self.password,
            }

        url = "{}/xrpc/{}".format(self.root, endpoint)
        response = self.extractor.request(
            url, method="POST", headers=headers, json=data, fatal=None)
        data = response.json()

        if response.status_code != 200:
            self.log.debug("Server response: %s", data)
            raise exception.AuthenticationError('"{}: {}"'.format(
                data.get("error"), data.get("message")))

        _refresh_token_cache.update(self.username, data["refreshJwt"])
        return "Bearer " + data["accessJwt"]

    def _call(self, endpoint, params):
        url = "{}/xrpc/{}".format(self.root, endpoint)

        while True:
            self.authenticate()
            response = self.extractor.request(
                url, params=params, headers=self.headers, fatal=None)

            if response.status_code < 400:
                return response.json()
            if response.status_code == 429:
                self.extractor.wait(seconds=60)
                continue

            self.extractor.log.debug("Server response: %s", response.text)
            raise exception.StopExtraction(
                "API request failed (%s %s)",
                response.status_code, response.reason)

    def _pagination(self, endpoint, params, key="feed"):
        while True:
            data = self._call(endpoint, params)
            yield from data[key]

            cursor = data.get("cursor")
            if not cursor:
                return
            params["cursor"] = cursor


@cache(maxage=84*86400, keyarg=0)
def _refresh_token_cache(username):
    return None
