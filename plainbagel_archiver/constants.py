# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Vaibhav Agarwal

from typing import Final

CHANNEL_URL: Final = "https://www.youtube.com/@ThePlainBagel"
CHANNEL_ID: Final = "UCFCEuCsyWP0YkP3CZ3Mr01Q"
CHANNEL_NAME: Final = "The Plain Bagel"
CREATOR_NAME: Final = "Richard Coffin"
CREATOR_EMAIL: Final = "richard@theplainbagel.com"
ARCHIVER_EMAIL: Final = "nekomeowpurr@proton.me"
DEFAULT_ARCHIVER_NAME: Final = "vaibhav"
DEFAULT_YTDLP_EXTRACTOR_ARGS: Final = ""

DEFAULT_COLLECTION: Final = "opensource_movies"
DEFAULT_IDENTIFIER_PREFIX: Final = "theplainbagel"

RENDITION_TARGETS: Final = ((720, "h264"), (720, "av1"), (480, "h264"), (480, "av1"))
CODEC_FALLBACKS: Final = {
    "av1": ("av1", "vp9", "vp8"),
    "h264": ("h264",),
}
MASTER_CODEC_PREFERENCE: Final = ("av1", "vp9", "h264", "vp8")
STANDARD_HEIGHTS: Final = (144, 240, 360, 480, 720, 1080, 1440, 2160, 4320)

CODE_CREATOR: Final = "Anonymous"
SOFTWARE_NAME: Final = "plainbagel-archiver"

RIGHTS_STATEMENT: Final = (
    "All rights reserved. Copyright remains with Richard Coffin and The Plain Bagel. "
    "This work is NOT in the public domain and is NOT offered under a Creative Commons "
    "license. The creator granted written permission for non-commercial archival and "
    "preservation use only. No commercial license is granted or implied. Commercial "
    "licensing inquiries must be directed to the rights holder."
)

PERMISSION_SUMMARY: Final = (
    "The creator, Richard Coffin of The Plain Bagel, granted written permission for "
    "non-commercial archival use. The creator retains all copyright and other rights. "
    "The complete permission correspondence is held privately by the archiver and can "
    "be provided to the Internet Archive or the rights holder on request."
)

PERMISSION_NOTE: Final = (
    "This item is a non-commercial preservation copy. It must not be presented as "
    "public domain. The archiver cannot grant licenses to the underlying work; only "
    "the rights holder can do that."
)

ARCHIVE_CONTACT_NOTE: Final = (
    "For questions about this archive, corrections, takedown requests, or additional "
    "permission information, contact the archiver. For questions about the original "
    "work or commercial licensing, contact the creator."
)

CREATOR_ATTRIBUTION: Final = "The Plain Bagel (Richard Coffin)"

SUBJECT_BASE: Final = (
    "The Plain Bagel",
    "Richard Coffin",
    "YouTube archive",
    "finance education",
    "investing",
    "personal finance",
)

DEFAULT_SUB_LANGS: Final = "en.*"
DEFAULT_CLEANUP: Final = True
DEFAULT_MAX_ATTEMPTS: Final = 4
DEFAULT_SESSION_MINUTES: Final = 600
DEFAULT_MIN_FREE_GB: Final = 12.0
DEFAULT_UPLOAD_RETRIES: Final = 8
DEFAULT_UPLOAD_RETRIES_SLEEP: Final = 30
DEFAULT_UPLOAD_TIMEOUT: Final = 1800
DEFAULT_VERIFY_TIMEOUT: Final = 600
DEFAULT_VERIFY_POLL: Final = 20

USER_AGENT_SUFFIX: Final = "plainbagel-archiver/1.0 (preservation)"
