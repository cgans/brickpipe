# Browser tests for the web page

These drive `brickpipe/web/index.html` in a real Chromium with Playwright. They serve the page from a
throwaway static server and stub every `/api/...` call inside the test, so they need no Nova, no
Rebrickable account and no network. They never touch your runs folder or real keys.

If `playwright` (or its Chromium) isn't installed, the whole `tests/ui` folder **skips with a message**;
the normal `tests/` run is unaffected.

## Install

```
pip install playwright
playwright install chromium
```

## Run

```
python -m pytest tests/ui            # just the UI suite
python -m pytest tests/ui -k egg     # one area
python -m pytest tests               # everything: the 140 unit tests plus the UI suite
python -m unittest discover -s tests/ui   # without pytest
```

On Windows (see `docs/testing-on-windows.md`) the commands are the same in PowerShell: use `py -3 -m pip ...`
and `py -3 -m pytest tests\ui`. The suite uses only portable paths, but it has so far been run on Linux only.

## What each test covers

`test_setup_dialog.py`: the first-visit "Get set up" dialog

- `FirstVisit.test_dialog_opens_on_first_visit`: the dialog is open on a first visit.
- `FirstVisit.test_skip_dismisses_and_stays_dismissed_after_reload`: Skip closes it and a reload doesn't reopen it.
- `FirstVisit.test_escape_dismisses_and_persists`: Escape does the same.
- `FirstVisit.test_done_persists`: Done does the same.
- `FirstVisit.test_open_setup_in_settings_reopens_it`: Settings > Open setup brings it back.
- `FirstVisit.test_skip_and_escape_do_not_record_a_key_choice`: only the shared-keys button sets `bp_key_choice`.
- `AiCard.test_provider_buttons_list_all_three`: Claude, OpenAI and OpenRouter buttons.
- `AiCard.test_provider_buttons_swap_placeholder_link_and_label`: each button changes the key placeholder, sign-up link and label line.
- `AiCard.test_save_posts_the_provider_correct_payload_for_each_provider`: Save sends the right `model`, `api_key` and `name` to `/api/nova/llm-models` for all three providers.
- `AiCard.test_empty_key_is_refused_without_a_request`: an empty key shows an error and sends nothing.
- `AiCard.test_status_covered_when_nova_has_a_default` / `test_status_needed_when_nova_has_no_default`: the AI status line.
- `RebrickableCards.test_key_card_says_needed_without_a_key` / `test_key_card_says_covered_when_the_server_has_a_key`: key card status.
- `RebrickableCards.test_saving_a_key_marks_it_saved_in_this_browser`: saving a key updates the status and storage.
- `RebrickableCards.test_collection_card_is_disabled_without_a_key`: the sign-in fields are disabled until a key exists.
- `RebrickableCards.test_collection_card_enables_after_a_key_and_signs_in`: with a key the card enables and the login request carries key, user and password.

`test_shared_keys_egg.py`: "Use the shared keys" and the easter egg

- `SharedKeysButton.test_button_is_a_primary_choice_distinct_from_skip`: a primary button with the explanatory sentence.
- `SharedKeysButton.test_button_uses_the_host_name_when_the_page_knows_it`: "Use Chris's shared keys" when `host_name` is set.
- `SharedKeysButton.test_click_records_the_choice_and_closes_the_dialog`: sets `bp_setup_done` and `bp_key_choice=defaults`, stays closed after reload.
- `EggFires.test_no_keys_shows_the_toast_with_just_the_words`: the toast shows a finger and only "Ah, ah, ah!".
- `EggFires.test_no_keys_marks_the_hook_and_tries_the_clip`: `window.bp_egg_fired` is set and `easter/nedry.mp3` was requested.
- `EggFires.test_missing_clip_falls_back_to_the_original_three_blips`: with no clip, three square-wave blips play within a second.
- `EggFires.test_toast_dismisses_itself_after_about_three_seconds`: still up at 1.5 s, gone shortly after 3 s.
- `EggFires.test_signed_in_collection_alone_counts_as_a_key`: a collection sign-in alone suppresses the egg.
- `EggAnimation.test_finger_wags_by_default`: the rocking animation runs.
- `EggReducedMotion.test_toast_still_shows_but_the_finger_does_not_rock`: with reduced motion the toast shows and the animation is off.
- `NoEggWithOwnKeys.*`: after saving an AI key, a Rebrickable key or a collection sign-in, the button closes the dialog and no egg fires (no toast, no hook, no clip request).
- `ClipPlays.test_present_clip_is_played_and_no_fallback_starts`: when a playable file exists at `easter/nedry.mp3`, it plays instead of the fallback.

The hook: after the egg fires, `window.bp_egg_fired` is `{src, audio}`; `audio` goes `pending`, then `file`, `synth` or
`silent`. It is also on the toast as `data-audio`. It stays `undefined` when no egg fired.

`test_menu_and_build.py`: the phone menu and the build form

- `HamburgerMenu.*` (393 px wide): hidden until opened, navigates and closes on choosing a view, closes on Escape or tapping elsewhere, toggles closed, no sideways scroll, and is hidden on a desktop window.
- `StartFromToggle.test_new_model_is_the_default`: New model is selected at first.
- `StartFromToggle.test_continue_reveals_the_selects_and_swaps_the_labels`: Continue shows the selects, "What should change?" and "Check and repair".
- `StartFromToggle.test_switching_back_resets_the_pick_and_restores_new_mode`: going back to New clears the model pick and restores the labels.
- `StartFromToggle.test_picking_a_model_selects_the_chat_that_made_it`: the chat follows the model.
- `StartFromToggle.test_new_model_submit_sends_a_prompt_and_no_model_or_chat` / `test_new_model_without_a_prompt_is_refused_with_no_request`: New mode payload and validation.
- `StartFromToggle.test_continue_submit_sends_the_model_and_chat_and_no_prompt` / `test_continue_with_nothing_picked_asks_for_the_latest_chat`: Continue mode payloads.
- `BricksStatusCard.*`: starts empty and collapsed, expands and collapses, the line count updates live (blank lines ignored), and typed lines go out with the run.

## The easter egg clip

The real clip isn't in the repo. To hear it, put your own file at `brickpipe/web/easter/nedry.mp3` (see
`brickpipe/web/easter/README.md`). The tests serve a copy of `index.html` only, so a local clip never changes their results.
