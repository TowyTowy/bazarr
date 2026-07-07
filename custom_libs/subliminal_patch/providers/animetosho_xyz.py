# -*- coding: utf-8 -*-
from __future__ import absolute_import

import logging
import lzma
import re

from guessit import guessit
from requests import Session
from subzero.language import Language

from subliminal.exceptions import ConfigurationError, ProviderError
from subliminal_patch.providers import Provider
from subliminal_patch.subtitle import Subtitle, guess_matches
from subliminal.video import Episode

logger = logging.getLogger(__name__)

FEED_BASE_URL = 'https://feed.animetosho.xyz'
VIEW_BASE_URL = 'https://animetosho.xyz/view'
DOWNLOAD_BASE_URL = 'https://animetosho.xyz/download'

# Matches subtitle links in the release view page:
# /download/{release_id}/subs/file/{file_id}">Label [lang, format]
_SUB_LINK_RE = re.compile(r'/download/(\d+)/subs/file/(\d+)[^"]*"[^>]*>([^<]+)')
_LANG_CODE_RE = re.compile(r'\[([a-z]{3}),')

supported_languages = [
    "ara",  # Arabic
    "eng",  # English
    "fin",  # Finnish
    "fra",  # French
    "deu",  # German
    "heb",  # Hebrew
    "ind",  # Indonesian
    "ita",  # Italian
    "jpn",  # Japanese
    "por",  # Portuguese
    "pol",  # Polish
    "rus",  # Russian
    "spa",  # Spanish
    "swe",  # Swedish
    "tha",  # Thai
    "tur",  # Turkish
    "vie",  # Vietnamese
]


class AnimeToshoXYZSubtitle(Subtitle):
    """AnimeTosho.xyz Subtitle."""
    provider_name = 'animetosho_xyz'

    def __init__(self, language, download_link, release_info):
        super(AnimeToshoXYZSubtitle, self).__init__(language, page_link=download_link)
        self.download_link = download_link
        self.release_info = release_info
        self.matches = set()

    @property
    def id(self):
        return self.download_link

    def get_matches(self, video):
        self.matches |= guess_matches(video, guessit(self.release_info))
        self.matches.update(['title', 'series', 'tvdb_id', 'season', 'episode'])
        return self.matches


class AnimeToshoXYZProvider(Provider):
    """AnimeTosho.xyz Provider."""
    subtitle_class = AnimeToshoXYZSubtitle
    languages = {Language('por', 'BR')} | {Language(sl) for sl in supported_languages}
    video_types = Episode

    def __init__(self, api_key=None, search_threshold=None):
        self.session = None

        if not api_key:
            raise ConfigurationError('API key must be specified')
        if not search_threshold:
            raise ConfigurationError('Search threshold must be specified')

        self.api_key = api_key
        self.search_threshold = search_threshold

    def initialize(self):
        self.session = Session()
        self.session.headers['X-API-Key'] = self.api_key

    def terminate(self):
        self.session.close()

    def list_subtitles(self, video, languages):
        if not video.series_anidb_episode_id:
            logger.debug('Skipping video %r. It is not an anime or the anidb_episode_id could not be identified', video)
            return []

        return [s for s in self._get_series(video.series_anidb_episode_id) if s.language in languages]

    def download_subtitle(self, subtitle):
        logger.info('Downloading subtitle %r', subtitle)

        r = self.session.get(subtitle.page_link, timeout=10)
        r.raise_for_status()

        if not self._is_xz_file(r.content):
            raise ProviderError('Unidentified archive type')

        subtitle.content = lzma.decompress(r.content)

        return subtitle

    @staticmethod
    def _is_xz_file(content):
        return content.startswith(b'\xFD\x37\x7A\x58\x5A\x00')

    def _get_series(self, episode_id):
        subtitles = []

        for entry in self._get_series_entries(episode_id):
            if not entry.get('metadata_fetched'):
                continue

            release_id = entry['id']
            release_info = entry.get('title', '')

            for lang, file_id in self._get_subtitle_files(release_id):
                download_url = f'{DOWNLOAD_BASE_URL}/{release_id}/subs/file/{file_id}'
                subtitle = self.subtitle_class(lang, download_url, release_info)
                logger.debug('Found subtitle %r', subtitle)
                subtitles.append(subtitle)

        return subtitles

    def _get_subtitle_files(self, release_id):
        r = self.session.get(f'{VIEW_BASE_URL}/{release_id}', timeout=10)
        if not r.ok:
            return []

        results = []
        for match in _SUB_LINK_RE.finditer(r.text):
            _, file_id, label = match.groups()
            lang_match = _LANG_CODE_RE.search(label)
            if not lang_match:
                continue

            try:
                lang = Language.fromalpha3b(lang_match.group(1))
            except Exception:
                continue

            if lang.alpha3 == 'por' and 'brazil' in label.lower():
                lang = Language('por', 'BR')

            results.append((lang, file_id))

        return results

    def _get_series_entries(self, episode_id):
        # episode_id may be a list; xyz API only supports one eid per request (multiple = AND, not OR)
        eids = episode_id if isinstance(episode_id, (list, tuple)) else [episode_id]

        seen = set()
        entries = []
        for eid in eids:
            r = self.session.get(
                f'{FEED_BASE_URL}/json/v1/releases',
                params={'eid': eid},
                timeout=10,
            )
            r.raise_for_status()
            data = r.json().get('data') or []
            if not isinstance(data, list):
                logger.warning('Unexpected response format from animetosho.xyz v1 API')
                continue
            for entry in data:
                if entry['id'] not in seen:
                    seen.add(entry['id'])
                    entries.append(entry)

        entries.sort(key=lambda t: t.get('date_added', ''), reverse=True)
        return entries[:self.search_threshold]
