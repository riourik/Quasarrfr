# -*- coding: utf-8 -*-
# Quasarrfr — Download source Movix/Darkiworld (1Fichier)
# Auteur : riourik

from quasarr.downloads.sources.helpers.abstract_source import AbstractDownloadSource
from quasarr.providers.hostname_issues import clear_hostname_issue
from quasarr.providers.log import debug


class Source(AbstractDownloadSource):
    initials = "mx"

    def get_download_links(self, shared_state, url, mirrors, title, password):
        # L'URL est déjà le lien 1Fichier direct (décodé lors de la recherche)
        if not url:
            return {"links": []}
        debug(f"[mx] download link: {url}")
        clear_hostname_issue(self.initials)
        return {"links": [[url, "1fichier.com"]]}
