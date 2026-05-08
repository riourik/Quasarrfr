# -*- coding: utf-8 -*-
# Quasarrfr — Source Movix/Darkiworld (DDL français, liens 1Fichier)
# Auteur : riourik

import time
from datetime import datetime, timezone

import requests

from quasarr.constants import (
    FEED_REQUEST_TIMEOUT_SECONDS,
    SEARCH_CAT_MOVIES,
    SEARCH_CAT_SHOWS,
    SEARCH_REQUEST_TIMEOUT_SECONDS,
)
from quasarr.providers import shared_state
from quasarr.providers.hostname_issues import clear_hostname_issue, mark_hostname_issue
from quasarr.providers.log import debug, error, warn
from quasarr.providers.utils import (
    generate_download_link,
    get_base_search_category_id,
    is_imdb_id,
)
from quasarr.search.sources.helpers.search_release import SearchRelease
from quasarr.search.sources.helpers.search_source import AbstractSearchSource

MOVIX_API = "https://api.movix.cash/api"
TMDB_API = "https://api.themoviedb.org/3"
TMDB_KEY = "f3d757824f08ea2cff45eb8f47ca3a1e"


class Source(AbstractSearchSource):
    """
    Source Movix/Darkiworld pour Quasarrfr.

    Flow :
      1. IMDb ID  →  TMDB  →  titre français
      2. GET /api/search?title=                          → darkiworld_id
      3. GET /api/darkiworld/download/{type}/{id}        → liste de liens
      4. GET /api/darkiworld/decode/{link_id}            → URL 1Fichier réelle
         → generate_download_link()  → JDownloader

    Configuration requise dans Quasarr (Settings > Hostnames) :
        mx  =  movix.cash
    """

    initials = "mx"
    supports_imdb = True
    supports_phrase = False
    supported_categories = [SEARCH_CAT_MOVIES, SEARCH_CAT_SHOWS]

    # ------------------------------------------------------------------ #
    #  HTTP                                                                #
    # ------------------------------------------------------------------ #

    def _get(self, base, path, params, shared_state_val, timeout):
        headers = {"User-Agent": shared_state_val.values["user_agent"],
                   "Referer": "https://movix.cash/",
                   "Origin": "https://movix.cash"}
        try:
            r = requests.get(f"{base}{path}", params=params, headers=headers, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            error(f"[mx] GET {path} — {e}")
            return None

    # ------------------------------------------------------------------ #
    #  Movix API                                                           #
    # ------------------------------------------------------------------ #

    def _search_title(self, title, ss, timeout):
        data = self._get(MOVIX_API, "/search", {"title": title}, ss, timeout)
        return data.get("results", []) if data else []

    def _get_links(self, darkiworld_id, tmdb_id, media_type, ss, timeout, season=None, episode=None):
        params = {"tmdbId": tmdb_id}
        if media_type == "tv":
            if not season or not episode:
                return []
            params["season"] = season
            params["episode"] = episode
        data = self._get(
            MOVIX_API,
            f"/darkiworld/download/{media_type}/{darkiworld_id}",
            params,
            ss,
            timeout,
        )
        if data and data.get("success"):
            return data.get("all", [])
        if data and not data.get("success"):
            debug(f"[mx] download {media_type}/{darkiworld_id}: {data.get('error', 'unknown error')}")
        return []

    def _decode_link(self, link_id, darkiworld_id, ss, timeout):
        data = self._get(
            MOVIX_API,
            f"/darkiworld/decode/{link_id}",
            {"title_id": darkiworld_id},
            ss,
            timeout,
        )
        if not data:
            return None
        return data.get("embed_url") or data.get("url") or data.get("link")

    # ------------------------------------------------------------------ #
    #  TMDB  (résolution IMDb ID → titre)                                  #
    # ------------------------------------------------------------------ #

    def _resolve_imdb(self, imdb_id, ss, timeout):
        data = self._get(
            TMDB_API,
            f"/find/{imdb_id}",
            {"api_key": TMDB_KEY, "external_source": "imdb_id", "language": "fr-FR"},
            ss,
            timeout,
        )
        if not data:
            return None, None
        for item in data.get("movie_results", []):
            return item.get("title") or item.get("original_title", ""), "movie"
        for item in data.get("tv_results", []):
            return item.get("name") or item.get("original_name", ""), "tv"
        return None, None

    def _trending_tmdb(self, media_type, ss, timeout):
        kind = "movie" if media_type == "movie" else "tv"
        data = self._get(
            TMDB_API,
            f"/trending/{kind}/week",
            {"api_key": TMDB_KEY, "language": "fr-FR"},
            ss,
            timeout,
        )
        return data.get("results", [])[:20] if data else []

    # ------------------------------------------------------------------ #
    #  Helpers                                                             #
    # ------------------------------------------------------------------ #

    def _best_match(self, results, imdb_id=None, tmdb_id=None):
        if imdb_id:
            for r in results:
                if r.get("imdb_id") == imdb_id:
                    return r
        if tmdb_id:
            for r in results:
                if str(r.get("tmdb_id")) == str(tmdb_id):
                    return r
        return results[0] if results else None

    @staticmethod
    def _to_rfc2822(date_str):
        try:
            dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.strftime("%a, %d %b %Y %H:%M:%S +0000")
        except Exception:
            return datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")

    def _build_releases(self, result, links, ss, timeout):
        releases = []
        darkiworld_id = result.get("id")
        r_title = result.get("name", "Unknown")
        r_year = result.get("year", "")
        r_imdb = result.get("imdb_id")
        r_tmdb = result.get("tmdb_id", "")
        media_type = "tv" if result.get("is_series") else "movie"

        for link in links:
            real_url = self._decode_link(link["id"], darkiworld_id, ss, timeout)
            if not real_url:
                debug(f"[mx] decode échoué pour link {link['id']}")
                continue

            quality = link.get("quality", "")
            host = link.get("host_name", "")
            language = link.get("language", "")
            size_bytes = link.get("size") or 0
            size_mb = round(size_bytes / (1024 * 1024), 2) if size_bytes else 0
            date_str = self._to_rfc2822(link.get("upload_date", ""))

            def sanitize(s):
                for ch in " :()'\"[]()":
                    s = s.replace(ch, ".")
                import re as _re
                s = _re.sub(r"\.{2,}", ".", s).strip(".")
                return s

            lang_tag = f".{sanitize(language)}" if language else ""
            host_tag = f".{sanitize(host)}" if host else ""
            ep_tag = ""
            if result.get("is_series"):
                saison = link.get("saison")
                ep = link.get("episode")
                if saison and ep:
                    ep_tag = f".S{int(saison):02d}E{int(ep):02d}"
            safe_title = sanitize(r_title)
            safe_quality = sanitize(quality)
            if result.get("is_series"):
                release_title = f"{safe_title}{ep_tag}.{safe_quality}.Movix{host_tag}{lang_tag}"
            else:
                release_title = f"{safe_title}.{r_year}.{safe_quality}.Movix{host_tag}{lang_tag}"

            source_url = f"https://movix.cash/download/{media_type}/{r_tmdb}"

            dl_link = generate_download_link(
                ss, release_title, real_url, size_mb, None, r_imdb, self.initials
            )

            releases.append({
                "details": {
                    "title": release_title,
                    "hostname": self.initials,
                    "imdb_id": r_imdb,
                    "link": dl_link,
                    "size": size_bytes,
                    "date": date_str,
                    "source": source_url,
                },
                "type": "protected",
            })
        return releases

    # ------------------------------------------------------------------ #
    #  Interface Quasarr                                                   #
    # ------------------------------------------------------------------ #

    def feed(
        self,
        shared_state: shared_state,
        start_time: float,
        search_category: str,
    ) -> list[SearchRelease]:

        base_cat = get_base_search_category_id(search_category)
        if base_cat == SEARCH_CAT_MOVIES:
            media_type = "movie"
        else:
            # TV requires season+episode params — feed mode can't provide them
            debug(f"[mx] feed: skip TV (season/episode requis par l'API)")
            return []

        releases = []
        try:
            trending = self._trending_tmdb(media_type, shared_state, FEED_REQUEST_TIMEOUT_SECONDS)

            for item in trending:
                title = item.get("title") or item.get("name", "")
                if not title:
                    continue

                results = self._search_title(title, shared_state, FEED_REQUEST_TIMEOUT_SECONDS)
                if not results:
                    continue

                match = self._best_match(results, tmdb_id=item.get("id"))
                if not match:
                    continue

                links = self._get_links(
                    match["id"], match.get("tmdb_id"), media_type,
                    shared_state, FEED_REQUEST_TIMEOUT_SECONDS
                )
                releases.extend(self._build_releases(match, links, shared_state, FEED_REQUEST_TIMEOUT_SECONDS))

            if releases:
                clear_hostname_issue(self.initials)
        except Exception as e:
            mark_hostname_issue(self.initials, "feed", str(e))
            warn(f"[mx] feed error: {e}")

        debug(f"[mx] feed: {len(releases)} releases — {time.time() - start_time:.2f}s")
        return releases

    def search(
        self,
        shared_state: shared_state,
        start_time: float,
        search_category: str,
        search_string: str = "",
        season: int = None,
        episode: int = None,
    ) -> list[SearchRelease]:

        if not search_string:
            return []

        base_cat = get_base_search_category_id(search_category)
        if base_cat == SEARCH_CAT_SHOWS:
            media_type = "tv"
        else:
            media_type = "movie"

        imdb_id = is_imdb_id(search_string)
        title = None

        if imdb_id:
            title, resolved_type = self._resolve_imdb(imdb_id, shared_state, SEARCH_REQUEST_TIMEOUT_SECONDS)
            if resolved_type:
                media_type = resolved_type
        else:
            title = search_string

        if not title:
            warn(f"[mx] impossible de résoudre: {search_string}")
            return []

        debug(f"[mx] recherche '{title}' [{media_type}] S{season}E{episode} — IMDb: {imdb_id}")

        releases = []
        try:
            results = self._search_title(title, shared_state, SEARCH_REQUEST_TIMEOUT_SECONDS)
            if not results:
                warn(f"[mx] aucun résultat Movix pour: {title}")
                return []

            match = self._best_match(results, imdb_id=imdb_id)
            if not match:
                return []

            links = self._get_links(
                match["id"], match.get("tmdb_id"), media_type,
                shared_state, SEARCH_REQUEST_TIMEOUT_SECONDS,
                season=season, episode=episode
            )
            releases = self._build_releases(match, links, shared_state, SEARCH_REQUEST_TIMEOUT_SECONDS)

            if releases:
                clear_hostname_issue(self.initials)
        except Exception as e:
            mark_hostname_issue(self.initials, "search", str(e))
            warn(f"[mx] search error: {e}")

        debug(f"[mx] {len(releases)} liens pour '{title}' — {time.time() - start_time:.2f}s")
        return releases
