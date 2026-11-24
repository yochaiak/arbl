"""Interact with the batch form's searchable pickers in browser smoke tests."""

import re

from playwright.sync_api import Locator, expect


def choose_batch_option(dialog: Locator, title: str, name: str, *, keyboard: bool = False):
    picker = dialog.get_by_role("button", name=title, exact=True)
    search = dialog.get_by_role("combobox", name=f"Find {title.lower()}", exact=True)
    expect(search).to_have_count(0)
    if keyboard:
        picker.press("ArrowDown")
    else:
        picker.click()
    expect(search).to_be_focused()
    search.fill(name)
    option = dialog.get_by_role("option", name=re.compile(rf"^{re.escape(name)}(?:\s|$)"))
    expect(option).to_be_visible()
    if keyboard:
        search.press("Enter")
        expect(picker).to_be_focused()
    else:
        option.click()
    expect(picker).to_have_attribute("aria-expanded", "false")
    expect(picker).to_contain_text(name)
