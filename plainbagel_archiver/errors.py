# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Vaibhav Agarwal

class ArchiverError(Exception):
    """Base class for expected archiver failures."""


class ConfigError(ArchiverError):
    pass


class DependencyError(ArchiverError):
    pass


class EnumerationError(ArchiverError):
    pass


class VideoUnavailableError(ArchiverError):
    pass


class VideoLiveError(ArchiverError):
    pass


class VideoUpcomingError(ArchiverError):
    pass


class FormatSelectionError(ArchiverError):
    pass


class DownloadError(ArchiverError):
    pass


class MediaError(ArchiverError):
    pass


class UploadError(ArchiverError):
    pass


class ItemCollisionError(UploadError):
    pass


class UploadVerificationError(UploadError):
    pass


class StateError(ArchiverError):
    pass


class LockError(ArchiverError):
    pass


class PauseRequested(ArchiverError):
    pass
