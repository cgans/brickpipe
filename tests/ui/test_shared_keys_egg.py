"""The "Use the shared keys" button and the Ah, ah, ah! easter egg.

Hook the tests read: after the egg fires, `window.bp_egg_fired` is `{src, audio}` where `src` is the clip
path that was tried and `audio` is `pending`, then `file` (the clip played), `synth` (the original WebAudio
fallback played) or `silent`. The same value is on the toast as `data-audio`. It is undefined when no egg fired.
"""
import os
import re
import struct
import wave


from ui_harness import UiCase, expect

SHOWN = re.compile(r"\bshow\b")

# Records every oscillator started, so a test can check the fallback is three short blips.
COUNT_OSCILLATORS = """
window.__osc = [];
const realStart = OscillatorNode.prototype.start;
OscillatorNode.prototype.start = function (t) { window.__osc.push({ at: t, type: this.type }); return realStart.apply(this, arguments); };
"""


class SharedKeysButton(UiCase):
    seen_setup = False

    def egg(self):
        return self.page.evaluate("window.bp_egg_fired")

    def wait_ready(self):
        self.open()
        expect(self.page.locator("#setupDlg")).to_be_visible()

    def test_button_is_a_primary_choice_distinct_from_skip(self):
        self.wait_ready()
        button = self.page.locator("#suDefaults")
        expect(button).to_be_visible()
        expect(button).to_have_class("btn primary")
        expect(button).to_have_text("Use the shared keys")  # no host name configured
        expect(self.page.locator("#suDefaultsNote")).to_contain_text("add your own later in Settings")
        self.assertNotEqual(button.evaluate("e => e.id"), "suSkip")

    def test_button_uses_the_host_name_when_the_page_knows_it(self):
        self.state.settings["host_name"] = "Chris"
        self.wait_ready()
        expect(self.page.locator("#suDefaults")).to_have_text("Use Chris's shared keys")
        expect(self.page.locator("#suDefaultsNote")).to_contain_text("Chris's accounts")

    def test_click_records_the_choice_and_closes_the_dialog(self):
        self.wait_ready()
        self.page.click("#suDefaults")
        expect(self.page.locator("#setupDlg")).not_to_be_visible()
        self.assertEqual(self.local("bp_setup_done"), "1")
        self.assertEqual(self.local("bp_key_choice"), "defaults")
        self.page.reload()
        self.page.wait_for_function("document.getElementById('version').textContent !== ''")
        expect(self.page.locator("#setupDlg")).not_to_be_visible()


class EggFires(UiCase):
    seen_setup = False

    def setUp(self):
        super().setUp()
        self.ctx.add_init_script(COUNT_OSCILLATORS)

    def press_defaults(self):
        self.open()
        self.page.click("#suDefaults")

    def test_no_keys_shows_the_toast_with_just_the_words(self):
        self.press_defaults()
        toast = self.page.locator("#eggToast")
        expect(toast).to_have_class(SHOWN)
        expect(toast).to_have_css("opacity", "1")
        expect(toast).to_have_text("☝️Ah, ah, ah!")
        expect(self.page.locator("#eggToast .finger")).to_be_visible()

    def test_no_keys_marks_the_hook_and_tries_the_clip(self):
        self.press_defaults()
        self.page.wait_for_function("window.bp_egg_fired")
        self.assertEqual(self.egg_src(), "easter/nedry.mp3")
        self.assertTrue(any(u.endswith("/easter/nedry.mp3") for u in self.requests), "the clip path was never requested")

    def egg_src(self):
        return self.page.evaluate("window.bp_egg_fired.src")

    def test_missing_clip_falls_back_to_the_original_three_blips(self):
        self.press_defaults()  # the test server has no easter/nedry.mp3, like a fresh checkout
        self.page.wait_for_function("window.bp_egg_fired && window.bp_egg_fired.audio !== 'pending'")
        self.assertEqual(self.page.evaluate("window.bp_egg_fired.audio"), "synth")
        expect(self.page.locator("#eggToast")).to_have_attribute("data-audio", "synth")
        osc = self.page.evaluate("window.__osc")
        self.assertEqual(len(osc), 3)
        self.assertTrue(all(o["type"] == "square" for o in osc))
        # three blips, the last one starting well inside 1.5 s
        self.assertLess(osc[-1]["at"] - osc[0]["at"], 1.0)

    def test_toast_dismisses_itself_after_about_three_seconds(self):
        self.press_defaults()
        toast = self.page.locator("#eggToast")
        expect(toast).to_have_class(SHOWN)
        self.page.wait_for_timeout(1500)
        expect(toast).to_have_class(SHOWN)  # still up at 1.5 s
        expect(toast).not_to_have_class(SHOWN, timeout=3000)  # gone by ~3 s (plus slack)

    def test_signed_in_collection_alone_counts_as_a_key(self):
        self.open()
        self.page.evaluate("localStorage.setItem('rebrickable_user_token', 'u')")
        self.page.click("#suDefaults")
        self.page.wait_for_timeout(300)
        self.assertIsNone(self.page.evaluate("window.bp_egg_fired"))


class EggAnimation(UiCase):
    seen_setup = False

    def finger_animation(self):
        return self.page.evaluate("getComputedStyle(document.querySelector('#eggToast .finger')).animationName")

    def test_finger_wags_by_default(self):
        self.open()
        self.page.click("#suDefaults")
        expect(self.page.locator("#eggToast")).to_have_class(SHOWN)
        self.assertEqual(self.finger_animation(), "wag")


class EggReducedMotion(UiCase):
    seen_setup = False
    context_options = {"reduced_motion": "reduce"}

    def test_toast_still_shows_but_the_finger_does_not_rock(self):
        self.open()
        self.page.click("#suDefaults")
        expect(self.page.locator("#eggToast")).to_have_class(SHOWN)
        expect(self.page.locator("#eggToast")).to_contain_text("Ah, ah, ah!")
        self.assertEqual(self.page.evaluate("getComputedStyle(document.querySelector('#eggToast .finger')).animationName"), "none")


class NoEggWithOwnKeys(UiCase):
    seen_setup = False

    def no_egg_after_pressing(self):
        self.page.click("#suDefaults")
        expect(self.page.locator("#setupDlg")).not_to_be_visible()
        self.page.wait_for_timeout(400)
        self.assertIsNone(self.page.evaluate("window.bp_egg_fired"))
        expect(self.page.locator("#eggToast")).not_to_have_class(re.compile(r"\bshow\b"))
        self.assertFalse(any(u.endswith("/easter/nedry.mp3") for u in self.requests))
        self.assertEqual(self.local("bp_key_choice"), "defaults")
        self.assertEqual(self.local("bp_setup_done"), "1")

    def test_after_adding_a_rebrickable_key_in_the_dialog(self):
        self.open()
        self.page.fill("#suRbKey", "rb-key")
        self.page.click("#suRbForm button[type=submit]")
        expect(self.page.locator("#suRbSt")).to_have_text("Saved in this browser")
        self.no_egg_after_pressing()

    def test_after_adding_an_ai_key_in_the_dialog(self):
        self.open()
        self.page.fill("#suAiKey", "sk-ant-mine")
        self.page.click("#suAiForm button[type=submit]")
        expect(self.page.locator("#suAiSt")).to_contain_text("Using your own key")
        self.no_egg_after_pressing()

    def test_after_signing_in_to_a_collection(self):
        self.open()
        self.page.fill("#suRbKey", "rb-key")
        self.page.click("#suRbForm button[type=submit]")
        self.page.fill("#suUser", "me")
        self.page.fill("#suPass", "pw")
        self.page.click("#suLoginBtn")
        expect(self.page.locator("#suLoginSt")).to_have_text("Signed in")
        self.no_egg_after_pressing()


class ClipPlays(UiCase):
    """When a playable clip is present, the egg plays it instead of the fallback."""
    seen_setup = False

    def test_present_clip_is_played_and_no_fallback_starts(self):
        # a real (silent) audio file served under the clip's name; the real clip is never in the repo
        silent = self._dir.name + "/easter"
        os.makedirs(silent, exist_ok=True)
        with wave.open(silent + "/nedry.mp3", "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000)
            w.writeframes(struct.pack("<h", 0) * 8000)
        self.ctx.add_init_script(COUNT_OSCILLATORS)
        self.open()
        self.page.click("#suDefaults")
        self.page.wait_for_function("window.bp_egg_fired && window.bp_egg_fired.audio !== 'pending'")
        self.assertEqual(self.page.evaluate("window.bp_egg_fired.audio"), "file")
        self.assertEqual(self.page.evaluate("window.__osc"), [])
