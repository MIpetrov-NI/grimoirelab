"""SortingHat settings for the local NI deployment."""

import os

from sortinghat.config.settings import *  # noqa: F403


DATA_UPLOAD_MAX_MEMORY_SIZE = int(
    os.environ.get("SORTINGHAT_DATA_UPLOAD_MAX_MEMORY_SIZE", 100 * 1024 * 1024)
)
