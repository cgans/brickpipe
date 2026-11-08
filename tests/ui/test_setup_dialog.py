"""The first-visit "Get set up" dialog: showing, dismissing, the AI provider card, and the key cards."""
import json


from ui_harness import UiCase, expect


class FirstVisit(UiCase):
    seen_setup = False

    def test_dialog_opens_on_first_visit(self):
        self.open()
        expect(self.page.locator("#setupDlg")).to_be_visible()
        expect(self.page.locator("#suTitle")).to_have_text("Get set up")

    def test_skip_dismisses_and_stays_dismissed_after_reload(self):
        self.open()
        self.page.click("#suSkip")
        expect(self.page.locator("#setupDlg")).not_to_be_visible()
        self.assertEqual(self.local("bp_setup_done"), "1")
        self.page.reload()
        self.page.wait_for_function("document.getElementById('version').textContent !== ''")
        expect(self.page.locator("#setupDlg")).not_to_be_visible()

    def test_escape_dismisses_and_persists(self):
        self.open()
        self.page.keyboard.press("Escape")
        expect(self.page.locator("#setupDlg")).not_to_be_visible()
        self.assertEqual(self.local("bp_setup_done"), "1")
        self.page.reload()
        self.page.wait_for_function("document.getElementById('version').textContent !== ''")
        expect(self.page.locator("#setupDlg")).not_to_be_visible()

    def test_done_persists(self):
        self.open()
        self.page.click("#suDone")
        expect(self.page.locator("#setupDlg")).not_to_be_visible()
        self.assertEqual(self.local("bp_setup_done"), "1")
        self.page.reload()
        self.page.wait_for_function("document.getElementById('version').textContent !== ''")
        expect(self.page.locator("#setupDlg")).not_to_be_visible()

    def test_open_setup_in_settings_reopens_it(self):
        self.open()
        self.page.click("#suDone")
        self.page.click("#tabSettings")
        self.page.click("#openSetup")
        expect(self.page.locator("#setupDlg")).to_be_visible()

    def test_skip_and_escape_do_not_record_a_key_choice(self):
        self.open()
        self.page.click("#suSkip")
        self.assertIsNone(self.local("bp_key_choice"))


class AiCard(UiCase):
    seen_setup = False

    def test_provider_buttons_list_all_three(self):
        self.open()
        self.assertEqual(self.page.locator("#suAiProv button").all_inner_texts(), ["Claude", "OpenAI", "OpenRouter"])

    def test_provider_buttons_swap_placeholder_link_and_label(self):
        self.open()
        expected = {
            "Claude": ("sk-ant-…", "platform.claude.com", "Add your own Claude key"),
            "OpenAI": ("sk-…", "platform.openai.com", "Add your own OpenAI key"),
            "OpenRouter": ("sk-or-…", "openrouter.ai", "Add your own OpenRouter key"),
        }
        for name, (placeholder, site, label) in expected.items():
            self.page.click(f"#suAiProv button:text-is('{name}')")
            expect(self.page.locator(f"#suAiProv button:text-is('{name}')")).to_have_attribute("aria-pressed", "true")
            expect(self.page.locator("#suAiKey")).to_have_attribute("placeholder", placeholder)
            expect(self.page.locator("#suAiNote a")).to_have_attribute("href", f"https://{site}/")
            expect(self.page.locator("#suAiLbl")).to_contain_text(label)

    def test_save_posts_the_provider_correct_payload_for_each_provider(self):
        self.open()
        for name, provider, model in [("Claude", "anthropic", "anthropic/claude-sonnet"), ("OpenAI", "openai", "openai/gpt-5"),
                                      ("OpenRouter", "openrouter", "openrouter/vendor/some-model")]:
            self.page.evaluate("() => { for (const k of ['llm_model_id', 'llm_model_name', 'llm_model_token']) localStorage.removeItem(k); }")
            self.page.reload()
            self.page.wait_for_function("document.getElementById('version').textContent !== ''")
            self.page.click(f"#suAiProv button:text-is('{name}')")
            self.page.fill("#suAiKey", f"key-for-{provider}")
            self.page.click("#suAiForm button[type=submit]")
            expect(self.page.locator("#suAiSt")).to_contain_text("Using your own key")
            body = self.state.posted("/api/nova/llm-models")[-1]
            self.assertEqual(body, {"model": model, "api_key": f"key-for-{provider}", "name": f"{model.split('/', 1)[1]} (own key)"})
            self.assertEqual(self.local("llm_model_id"), "k-" + provider)
        self.assertEqual(len(self.state.posted("/api/nova/llm-models")), 3)

    def test_empty_key_is_refused_without_a_request(self):
        self.open()
        self.page.click("#suAiForm button[type=submit]")
        expect(self.page.locator("#suAiErr")).to_have_text("Paste a key first.")
        self.assertEqual(self.state.posted("/api/nova/llm-models"), [])

    def test_status_covered_when_nova_has_a_default(self):
        self.open()
        expect(self.page.locator("#suAiSt")).to_contain_text("Covered")

    def test_status_needed_when_nova_has_no_default(self):
        self.state.accounts = {"default_id": None, "models": []}
        self.open()
        expect(self.page.locator("#suAiSt")).to_contain_text("Needed")


class RebrickableCards(UiCase):
    seen_setup = False

    def test_key_card_says_needed_without_a_key(self):
        self.open()
        expect(self.page.locator("#suRbSt")).to_contain_text("Needed")
        expect(self.page.locator("#suRbForm")).to_be_visible()

    def test_key_card_says_covered_when_the_server_has_a_key(self):
        self.state.settings["has_server_key"] = True
        self.open()
        expect(self.page.locator("#suRbSt")).to_have_text("Covered by this server")
        expect(self.page.locator("#suRbForm")).to_be_hidden()

    def test_saving_a_key_marks_it_saved_in_this_browser(self):
        self.open()
        self.page.fill("#suRbKey", "rb-key")
        self.page.click("#suRbForm button[type=submit]")
        expect(self.page.locator("#suRbSt")).to_have_text("Saved in this browser")
        self.assertEqual(self.local("rebrickable_key"), "rb-key")

    def test_collection_card_is_disabled_without_a_key(self):
        self.open()
        for sel in ("#suUser", "#suPass", "#suLoginBtn"):
            expect(self.page.locator(sel)).to_be_disabled()
        expect(self.page.locator("#suLoginSt")).to_contain_text("Needs the Rebrickable API key")

    def test_collection_card_enables_after_a_key_and_signs_in(self):
        self.open()
        self.page.fill("#suRbKey", "rb-key")
        self.page.click("#suRbForm button[type=submit]")
        for sel in ("#suUser", "#suPass", "#suLoginBtn"):
            expect(self.page.locator(sel)).to_be_enabled()
        self.page.fill("#suUser", "brick@example.com")
        self.page.fill("#suPass", "hunter2")
        self.page.click("#suLoginBtn")
        expect(self.page.locator("#suLoginSt")).to_have_text("Signed in")
        body = self.state.posted("/api/rebrickable/login")[0]
        self.assertEqual(json.dumps(body, sort_keys=True), json.dumps(
            {"api_key": "rb-key", "password": "hunter2", "username": "brick@example.com"}, sort_keys=True))
