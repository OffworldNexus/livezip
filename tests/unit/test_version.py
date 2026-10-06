"""Guards that the advertised version stays consistent with the package.

The release workflow also checks that the pushed tag matches
``project.version``; this test catches the same drift locally, before a tag is
ever created.
"""

from importlib.metadata import version

import livezip


def test_version_matches_installed_metadata():
    assert version("livezip") == livezip.__version__
