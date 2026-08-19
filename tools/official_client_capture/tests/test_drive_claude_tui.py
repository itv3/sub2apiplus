"""Claude 2.1.226 TUI 严格状态识别测试。"""

from __future__ import annotations

import unittest

from tools.official_client_capture.drive_claude_tui import classify_screen, clean


class ClaudeTUIScreenStateTest(unittest.TestCase):
    def test_trust_menu_pointer_is_not_main_input(self) -> None:
        screen = "Do you trust the files in this folder?\n❯ I trust this folder"
        self.assertEqual(classify_screen(screen), "menu")

    def test_login_menu_is_fatal_auth_gate(self) -> None:
        screen = "Select login method\n❯ Claude account with subscription"
        self.assertEqual(classify_screen(screen), "auth_gate")

    def test_busy_footer_is_not_main_input(self) -> None:
        screen = "Working…\nesc to interrupt"
        self.assertEqual(classify_screen(screen), "busy")

    def test_new_busy_footer_overrides_old_idle_footer(self) -> None:
        screen = "❯\n? for shortcuts\nWorking…\nesc to interrupt"
        self.assertEqual(classify_screen(screen), "busy")

    def test_pointer_and_shortcut_footer_are_idle_main_input(self) -> None:
        screen = "❯\n? for shortcuts"
        self.assertEqual(classify_screen(screen), "idle_prompt")

    def test_new_idle_footer_overrides_old_busy_footer_without_redrawn_pointer(self) -> None:
        screen = "❯\nesc to interrupt\nFW_F_V4_OK\n? for shortcuts"
        self.assertEqual(classify_screen(screen), "idle_prompt")

    def test_pointer_alone_is_not_main_input(self) -> None:
        self.assertEqual(classify_screen("❯"), "waiting")

    def test_ansi_private_csi_is_removed_before_state_detection(self) -> None:
        wire = b"\x1b[>0q\xe2\x9d\xaf\r\n? for shortcuts"
        self.assertEqual(classify_screen(clean(wire)), "idle_prompt")


if __name__ == "__main__":
    unittest.main()
