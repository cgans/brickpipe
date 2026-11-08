"""The phone menu, the New / Continue toggle on the build form, and the bricks status card."""

from ui_harness import UiCase, expect


class HamburgerMenu(UiCase):
    viewport = {"width": 393, "height": 800}

    def test_nav_is_hidden_until_the_menu_button_opens_it(self):
        self.open()
        expect(self.page.locator("#menuBtn")).to_be_visible()
        expect(self.page.locator("#mainNav")).to_be_hidden()
        self.page.click("#menuBtn")
        expect(self.page.locator("#mainNav")).to_be_visible()
        expect(self.page.locator("#menuBtn")).to_have_attribute("aria-expanded", "true")

    def test_choosing_a_view_navigates_and_closes_the_menu(self):
        self.open()
        for tab, view in [("tabSettings", "settingsView"), ("tabHistory", "historyView")]:
            self.page.click("#menuBtn")
            self.page.click("#" + tab)
            expect(self.page.locator("#" + view)).to_be_visible()
            expect(self.page.locator("#mainNav")).to_be_hidden()
            expect(self.page.locator("#menuBtn")).to_have_attribute("aria-expanded", "false")
        self.page.click("#menuBtn")
        self.page.click("#tabBuild")
        expect(self.page.locator("#briefForm")).to_be_visible()

    def test_escape_and_tapping_elsewhere_close_it(self):
        self.open()
        self.page.click("#menuBtn")
        self.page.keyboard.press("Escape")
        expect(self.page.locator("#mainNav")).to_be_hidden()
        self.page.click("#menuBtn")
        self.page.click("#prompt")
        expect(self.page.locator("#mainNav")).to_be_hidden()

    def test_menu_button_toggles_closed_again(self):
        self.open()
        self.page.click("#menuBtn")
        self.page.click("#menuBtn")
        expect(self.page.locator("#mainNav")).to_be_hidden()

    def test_no_horizontal_scroll_at_phone_width(self):
        self.open()
        self.assertLessEqual(self.page.evaluate("document.documentElement.scrollWidth"), 393)

    def test_menu_button_is_hidden_on_a_desktop_window(self):
        self.page.set_viewport_size({"width": 1100, "height": 800})
        self.open()
        expect(self.page.locator("#menuBtn")).to_be_hidden()
        expect(self.page.locator("#mainNav")).to_be_visible()


class StartFromToggle(UiCase):
    def continue_mode(self):
        self.page.click("#segCont")
        expect(self.page.locator("#existing")).to_be_visible()

    def test_new_model_is_the_default(self):
        self.open()
        expect(self.page.locator("#segNew")).to_have_attribute("aria-pressed", "true")
        expect(self.page.locator("#existing")).to_be_hidden()
        expect(self.page.locator("#promptLabel")).to_have_text("What should Nova build?")
        expect(self.page.locator("#runBtn")).to_have_text("Design and check")

    def test_continue_reveals_the_selects_and_swaps_the_labels(self):
        self.open()
        self.continue_mode()
        expect(self.page.locator("#segCont")).to_have_attribute("aria-pressed", "true")
        expect(self.page.locator("#promptLabel")).to_have_text("What should change?")
        expect(self.page.locator("#runBtn")).to_have_text("Check and repair")
        expect(self.page.locator("#promptNote")).to_be_visible()
        expect(self.page.locator("#modelFile option")).to_have_text(
            ["Newest model from the chosen chat", "Shed (150 parts)", "Tower (80 parts)"])

    def test_switching_back_resets_the_pick_and_restores_new_mode(self):
        self.open()
        self.continue_mode()
        self.page.select_option("#modelFile", "tower.mpd")
        self.assertEqual(self.page.input_value("#chatId"), "c2")
        self.page.click("#segNew")
        expect(self.page.locator("#existing")).to_be_hidden()
        expect(self.page.locator("#promptLabel")).to_have_text("What should Nova build?")
        expect(self.page.locator("#runBtn")).to_have_text("Design and check")
        expect(self.page.locator("#promptNote")).to_be_hidden()
        self.assertEqual(self.page.input_value("#modelFile"), "")

    def test_picking_a_model_selects_the_chat_that_made_it(self):
        self.open()
        self.continue_mode()
        self.page.select_option("#modelFile", "shed.mpd")
        self.assertEqual(self.page.input_value("#chatId"), "c1")
        expect(self.page.locator("#chatNote")).to_contain_text("Shed chat")

    def test_new_model_submit_sends_a_prompt_and_no_model_or_chat(self):
        self.open()
        self.continue_mode()
        self.page.select_option("#modelFile", "tower.mpd")
        self.page.click("#segNew")  # an earlier pick must not leak into a fresh design
        self.page.fill("#prompt", "a red shed")
        self.page.click("#runBtn")
        self.page.wait_for_function("document.getElementById('runSheet').hidden === false")
        body = self.state.posted("/api/runs")[0]
        self.assertEqual(body["prompt"], "a red shed")
        self.assertNotIn("model_file", body)
        self.assertNotIn("chat_id", body)

    def test_new_model_without_a_prompt_is_refused_with_no_request(self):
        self.open()
        self.page.click("#runBtn")
        expect(self.page.locator("#formError")).to_contain_text("Describe the model first")
        self.assertEqual(self.state.posted("/api/runs"), [])

    def test_continue_submit_sends_the_model_and_chat_and_no_prompt(self):
        self.open()
        self.continue_mode()
        self.page.select_option("#modelFile", "shed.mpd")
        self.page.fill("#prompt", "add a chimney")
        self.page.click("#runBtn")
        self.page.wait_for_function("document.getElementById('runSheet').hidden === false")
        body = self.state.posted("/api/runs")[0]
        self.assertEqual((body["model_file"], body["chat_id"]), ("shed.mpd", "c1"))
        self.assertNotIn("prompt", body)

    def test_continue_with_nothing_picked_asks_for_the_latest_chat(self):
        self.open()
        self.continue_mode()
        self.page.click("#runBtn")
        self.page.wait_for_function("document.getElementById('runSheet').hidden === false")
        body = self.state.posted("/api/runs")[0]
        self.assertEqual((body["model_file"], body["chat_id"]), ("", "latest"))


class BricksStatusCard(UiCase):
    def test_starts_empty_and_collapsed(self):
        self.open()
        expect(self.page.locator("#ownedStatus")).to_have_text("Nothing entered — you'll shop for every piece")
        expect(self.page.locator("#ownedBtn")).to_have_attribute("aria-expanded", "false")
        expect(self.page.locator("#ownedBody")).to_be_hidden()

    def test_card_expands_and_collapses(self):
        self.open()
        self.page.click("#ownedBtn")
        expect(self.page.locator("#ownedBody")).to_be_visible()
        expect(self.page.locator("#ownedBtn")).to_have_attribute("aria-expanded", "true")
        self.page.click("#ownedBtn")
        expect(self.page.locator("#ownedBody")).to_be_hidden()

    def test_count_updates_live_as_lines_are_typed(self):
        self.open()
        self.page.click("#ownedBtn")
        status = self.page.locator("#ownedStatus")
        self.page.fill("#ownedText", "3001, Red, 12")
        expect(status).to_contain_text("1 line entered")
        self.page.fill("#ownedText", "3001, Red, 12\n3024, White, 30\n\n   \n3005, , 8")
        expect(status).to_contain_text("3 lines entered")  # blank lines are not counted
        expect(status).to_have_class("st has")
        self.page.fill("#ownedText", "")
        expect(status).to_have_text("Nothing entered — you'll shop for every piece")
        expect(status).to_have_class("st")

    def test_typed_lines_go_out_with_the_run(self):
        self.open()
        self.page.click("#ownedBtn")
        self.page.fill("#ownedText", "3001, Red, 12")
        self.page.fill("#prompt", "a wall")
        self.page.click("#runBtn")
        self.page.wait_for_function("document.getElementById('runSheet').hidden === false")
        self.assertEqual(self.state.posted("/api/runs")[0]["owned_text"], "3001, Red, 12")
