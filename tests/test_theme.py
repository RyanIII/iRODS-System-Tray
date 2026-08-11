"""Tests for the theme values the app loads at startup.

``THEME_TOKENS`` in main.py holds one color per name for the light theme and the
dark theme. ``theme.qss.template`` refers to those names, and ``_apply_theme``
substitutes them in to build the Qt stylesheet before the first window is shown.
"""

from __future__ import annotations

from string import Template

import pytest

from main import THEME_TEMPLATE_PATH, THEME_TOKENS

THEMES = sorted(THEME_TOKENS)


@pytest.mark.parametrize("theme", THEMES)
def test_stylesheet_builds_for_every_theme(theme):
    # The app fills in the stylesheet this way when it starts. If the stylesheet asks
    # for a color that no longer has a value, this step fails and the app never gets
    # as far as opening a window. The two files are edited separately, so adding a
    # rule without adding its color is easy to do.
    rendered = Template(THEME_TEMPLATE_PATH.read_text()).substitute(THEME_TOKENS[theme])

    assert "$" not in rendered
