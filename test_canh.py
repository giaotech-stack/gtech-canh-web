import contextlib
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import canh

T0 = datetime(2026, 1, 1, 3, 0, tzinfo=timezone.utc)
FAKE_TOKEN = "123456789:AAAbbbCCCdddEEEfffGGGhhhIII_jjj-KK"
FAKE_URL = "https://example.com/api/health"


def minutes(n):
    return T0 + timedelta(minutes=n)


class FakeNet:
    """Giả lập fetch + Telegram. `responses`: danh sách (status, body) hoặc Exception, dùng vòng."""

    def __init__(self, responses, telegram_ok=True):
        self.responses = responses
        self.calls = []
        self.sent = []
        self.telegram_ok = telegram_ok

    def fetch(self, url, timeout=20):
        self.calls.append(url)
        item = self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]
        if isinstance(item, Exception):
            raise item
        return item

    def send(self, token, chat_id, text):
        self.sent.append(text)
        return self.telegram_ok


OK = (200, b'{"status":"ok","db":"ok"}')
DB_DOWN = (503, b'{"status":"error","db":"down"}')


def run_main(net, state_path, targets=f"Dịch vụ A|{FAKE_URL}", now=T0, extra_env=None):
    env = {
        "TARGETS": targets,
        "TELEGRAM_BOT_TOKEN": FAKE_TOKEN,
        "TELEGRAM_CHAT_ID": "987654321",
    }
    env.update(extra_env or {})
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        code = canh.main([state_path], env, fetch_fn=net.fetch, send_fn=net.send,
                         sleep_fn=lambda s: None, now_fn=lambda: now)
    return code, out.getvalue()


class ParseTargets(unittest.TestCase):
    def test_ok_with_blank_lines(self):
        raw = f"\n  A | {FAKE_URL}  \n\nB|https://example.org/h\n"
        self.assertEqual(canh.parse_targets(raw),
                         [("A", FAKE_URL), ("B", "https://example.org/h")])

    def test_bad_line_reports_line_number_without_content(self):
        with self.assertRaises(canh.ConfigError) as cm:
            canh.parse_targets(f"A|{FAKE_URL}\nrac-roi-bi-mat")
        msg = str(cm.exception)
        self.assertIn("dòng 2", msg)
        self.assertNotIn("rac-roi-bi-mat", msg)

    def test_bad_scheme_and_empty(self):
        with self.assertRaises(canh.ConfigError):
            canh.parse_targets("A|ftp://example.com/x")
        with self.assertRaises(canh.ConfigError):
            canh.parse_targets("  \n\n")
        with self.assertRaises(canh.ConfigError):
            canh.parse_targets("|https://example.com/x")


class Check(unittest.TestCase):
    def test_up(self):
        net = FakeNet([OK])
        self.assertEqual(canh.check_target(FAKE_URL, net.fetch, lambda s: None), (True, "HTTP 200"))
        self.assertEqual(len(net.calls), 1)

    def test_503_retries_twice_then_down(self):
        net = FakeNet([DB_DOWN])
        sleeps = []
        up, detail = canh.check_target(FAKE_URL, net.fetch, sleeps.append)
        self.assertEqual((up, detail), (False, "HTTP 503"))
        self.assertEqual(len(net.calls), 3)
        self.assertEqual(sleeps, [20, 20])

    def test_recovers_on_retry(self):
        net = FakeNet([DB_DOWN, OK])
        self.assertTrue(canh.check_target(FAKE_URL, net.fetch, lambda s: None)[0])

    def test_invalid_json_and_wrong_status(self):
        net = FakeNet([(200, b"<html>")])
        self.assertEqual(canh.check_target(FAKE_URL, net.fetch, lambda s: None),
                         (False, "phản hồi không hợp lệ"))
        net = FakeNet([(200, b'{"status":"degraded"}')])
        self.assertFalse(canh.check_target(FAKE_URL, net.fetch, lambda s: None)[0])

    def test_timeout_and_connection_error(self):
        net = FakeNet([TimeoutError(FAKE_URL)])
        self.assertEqual(canh.check_target(FAKE_URL, net.fetch, lambda s: None), (False, "timeout"))
        net = FakeNet([OSError("boom " + FAKE_URL)])
        self.assertEqual(canh.check_target(FAKE_URL, net.fetch, lambda s: None),
                         (False, "lỗi kết nối"))


class CheckRobust(unittest.TestCase):
    def test_deeply_nested_json_is_invalid_not_crash(self):
        net = FakeNet([(200, b"[" * 100000)])
        self.assertEqual(canh.check_once(FAKE_URL, net.fetch), (False, "phản hồi không hợp lệ"))

    def test_bom_is_accepted(self):
        net = FakeNet([(200, b'\xef\xbb\xbf{"status":"ok"}')])
        self.assertEqual(canh.check_once(FAKE_URL, net.fetch), (True, "HTTP 200"))

    def test_bad_bytes_and_non_bytes_body_invalid(self):
        for body in (b"\xff\xfe\x00", "khong phai bytes", None):
            net = FakeNet([(200, body)])
            self.assertEqual(canh.check_once(FAKE_URL, net.fetch), (False, "phản hồi không hợp lệ"))

    def test_unexpected_exception_never_escapes_and_logs_class_only(self):
        class Weird(Exception):
            def __str__(self):
                raise RuntimeError(FAKE_URL)  # ngay cả str() cũng nổ

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            net = FakeNet([Weird(FAKE_URL)])
            result = canh.check_once(FAKE_URL, net.fetch)
            bad_shape = canh.check_once(FAKE_URL, lambda url, timeout=1: "khong phai tuple")
        self.assertEqual(result[0], False)
        self.assertEqual(bad_shape[0], False)
        self.assertNotIn(FAKE_URL, out.getvalue())

    def test_outer_safety_net_reports_class_name_only(self):
        class BadStatus:
            def __ne__(self, other):
                raise ZeroDivisionError(FAKE_URL)

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = canh.check_once(FAKE_URL, lambda url, timeout=1: (BadStatus(), b""))
        self.assertEqual(result, (False, "lỗi kiểm tra"))
        self.assertIn("ZeroDivisionError", out.getvalue())
        self.assertNotIn(FAKE_URL, out.getvalue())

    def test_unexpected_exception_in_check_target_is_down_in_main_loop(self):
        def boom(url, fetch_fn, sleep_fn):
            raise ValueError(FAKE_URL)

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "s.json")
        net = FakeNet([OK])
        original = canh.check_target
        canh.check_target = boom
        try:
            code, out = run_main(net, path)
        finally:
            canh.check_target = original
        self.assertEqual(code, 0)
        self.assertIn("target #1: DOWN (lỗi kiểm tra)", out)
        self.assertIn("ValueError", out)
        self.assertNotIn(FAKE_URL, out)
        self.assertEqual(len(net.sent), 1)


class Deadline(unittest.TestCase):
    """Đồng hồ giả: fetch tốn đúng `timeout` giây, sleep tốn `s` giây."""

    def setUp(self):
        self.t = 0.0
        self.timeouts = []

    def clock(self):
        return self.t

    def sleep(self, s):
        self.t += s

    def slow_fetch(self, url, timeout=20):
        self.timeouts.append(timeout)
        self.t += timeout
        raise TimeoutError()

    def test_total_never_exceeds_budget_and_last_timeout_shrinks(self):
        up, detail = canh.check_target(FAKE_URL, self.slow_fetch, self.sleep, clock=self.clock)
        self.assertEqual((up, detail), (False, "timeout"))
        self.assertLessEqual(self.t, canh.TARGET_BUDGET)
        self.assertTrue(all(t <= canh.TIMEOUT for t in self.timeouts))

    def test_skips_retry_when_not_enough_time(self):
        # budget 45: lần 1 (20s) + chờ 20s => còn 5s, đủ cho lần 2 với timeout rút còn 5s
        canh.check_target(FAKE_URL, self.slow_fetch, self.sleep, budget=45, clock=self.clock)
        self.assertEqual(self.timeouts, [20, 5])
        self.assertLessEqual(self.t, 45)
        # budget 30: sau lần 1 chỉ còn 10s < chờ 20s + tối thiểu => không thử thêm
        self.t, self.timeouts = 0.0, []
        canh.check_target(FAKE_URL, self.slow_fetch, self.sleep, budget=30, clock=self.clock)
        self.assertEqual(self.timeouts, [20])
        self.assertEqual(self.t, 20)

    def test_fast_failures_still_get_all_three_attempts(self):
        net = FakeNet([DB_DOWN])
        canh.check_target(FAKE_URL, net.fetch, self.sleep, clock=self.clock)
        self.assertEqual(len(net.calls), 3)  # 0 + 20 + 0 + 20 + 0 = 40s < 75s

    def test_first_attempt_timeout_is_min_of_20_and_remaining(self):
        canh.check_target(FAKE_URL, self.slow_fetch, self.sleep, budget=10, clock=self.clock)
        self.assertEqual(self.timeouts, [10])


class Decide(unittest.TestCase):
    def test_first_run_up_no_alert(self):
        kind, st = canh.decide(None, True, T0)
        self.assertIsNone(kind)
        self.assertTrue(st["up"])

    def test_first_run_down_alerts(self):
        kind, st = canh.decide(None, False, T0)
        self.assertEqual(kind, "down")
        self.assertFalse(st["up"])

    def test_up_to_down(self):
        prev = {"up": True, "since": T0.isoformat(), "last_alert": None}
        kind, st = canh.decide(prev, False, minutes(15))
        self.assertEqual(kind, "down")
        self.assertEqual(st["since"], minutes(15).isoformat())

    def test_reminder_only_after_60_minutes(self):
        prev = {"up": False, "since": T0.isoformat(), "last_alert": T0.isoformat()}
        self.assertIsNone(canh.decide(prev, False, minutes(30))[0])
        self.assertIsNone(canh.decide(prev, False, minutes(45))[0])
        kind, st = canh.decide(prev, False, minutes(60))
        self.assertEqual(kind, "reminder")
        self.assertEqual(st["last_alert"], minutes(60).isoformat())
        self.assertEqual(st["since"], T0.isoformat())  # since giữ nguyên
        # lệch lịch nhẹ (58') vẫn nhắc; sau khi nhắc thì im lại
        self.assertEqual(canh.decide(prev, False, minutes(58))[0], "reminder")
        self.assertIsNone(canh.decide(st, False, minutes(75))[0])

    def test_down_to_up_with_duration(self):
        prev = {"up": False, "since": T0.isoformat(), "last_alert": T0.isoformat()}
        now = T0 + timedelta(hours=2, minutes=15)
        kind, st = canh.decide(prev, True, now)
        self.assertEqual(kind, "up")
        self.assertTrue(st["up"])
        msg = canh.build_message("up", "A", "HTTP 200", now, prev)
        self.assertIn("🟢 A đã hoạt động lại", msg)
        self.assertIn("2 giờ 15 phút", msg)

    def test_duration_format(self):
        self.assertEqual(canh.fmt_duration(timedelta(seconds=20)), "dưới 1 phút")
        self.assertEqual(canh.fmt_duration(timedelta(days=1, hours=2)), "1 ngày 2 giờ")

    def test_message_uses_vn_time(self):
        msg = canh.build_message("down", "A", "HTTP 503", T0, None)  # 03:00 UTC = 10:00 VN
        self.assertIn("🔴 A KHÔNG phản hồi", msg)
        self.assertIn("HTTP 503", msg)
        self.assertIn("10:00 01/01/2026", msg)


class Redact(unittest.TestCase):
    def test_redacts_token_digits_and_url(self):
        text = f"tok {FAKE_TOKEN} sdt 0900000000 id 123456789 url {FAKE_URL} ok 12345678"
        out = canh.redact(text)
        self.assertNotIn(FAKE_TOKEN, out)
        self.assertNotIn("AAAbbb", out)
        self.assertNotIn("0900000000", out)
        self.assertNotIn("123456789", out)
        self.assertNotIn("example.com", out)
        self.assertIn("12345678", out)  # 8 chữ số: giữ nguyên


class MainFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "sub", "state.json")

    def state(self):
        with open(self.path, encoding="utf-8") as fh:
            return json.load(fh)

    def test_first_run_down_alerts_exit_0_and_persists(self):
        net = FakeNet([DB_DOWN])
        code, out = run_main(net, self.path)
        self.assertEqual(code, 0)
        self.assertEqual(len(net.sent), 1)
        self.assertIn("KHÔNG phản hồi", net.sent[0])
        self.assertFalse(self.state()["1"]["up"])
        self.assertIn("state #1 chưa có, coi như lần đầu", out)
        self.assertIn("target #1: DOWN (HTTP 503)", out)
        self.assertIn("alert sent: yes", out)

    def test_first_run_up_silent(self):
        net = FakeNet([OK])
        code, out = run_main(net, self.path)
        self.assertEqual((code, net.sent), (0, []))
        self.assertTrue(self.state()["1"]["up"])
        self.assertIn("target #1: UP (HTTP 200)", out)

    def test_full_cycle_down_reminder_up(self):
        run_main(FakeNet([OK]), self.path, now=T0)
        net = FakeNet([DB_DOWN])
        run_main(net, self.path, now=minutes(15))
        self.assertEqual(len(net.sent), 1)
        net = FakeNet([DB_DOWN])
        run_main(net, self.path, now=minutes(30))
        self.assertEqual(net.sent, [])
        run_main(net, self.path, now=minutes(75))  # 60' sau lần báo lúc 15'
        self.assertEqual(len(net.sent), 1)
        self.assertIn("vẫn KHÔNG phản hồi", net.sent[0])
        net = FakeNet([OK])
        run_main(net, self.path, now=minutes(135))
        self.assertIn("đã hoạt động lại", net.sent[0])
        self.assertIn("2 giờ", net.sent[0])

    def test_telegram_failure_exit_1_and_state_not_advanced(self):
        net = FakeNet([DB_DOWN], telegram_ok=False)
        code, out = run_main(net, self.path)
        self.assertEqual(code, 1)
        self.assertIn("alert sent: no", out)
        entry = self.state()["1"]  # ghi nhận lần phát hiện đầu nhưng CHƯA báo được
        self.assertEqual((entry["up"], entry["alerted"], entry["since"]),
                         (False, False, T0.isoformat()))

    def test_failed_first_down_retried_keeps_original_since(self):
        run_main(FakeNet([DB_DOWN], telegram_ok=False), self.path, now=T0)
        net = FakeNet([DB_DOWN])
        code, _ = run_main(net, self.path, now=minutes(15))
        self.assertEqual(code, 0)
        self.assertEqual(len(net.sent), 1)
        self.assertIn("Phát hiện từ: 10:00 01/01/2026", net.sent[0])
        entry = self.state()["1"]
        self.assertEqual((entry["alerted"], entry["since"]), (True, T0.isoformat()))
        # sau đó hồi phục -> báo 🟢 tính từ lần phát hiện đầu
        net = FakeNet([OK])
        run_main(net, self.path, now=minutes(75))
        self.assertIn("1 giờ 15 phút", net.sent[0])

    def test_unalerted_down_then_recovered_sends_nothing(self):
        run_main(FakeNet([DB_DOWN], telegram_ok=False), self.path, now=T0)
        net = FakeNet([OK])
        code, _ = run_main(net, self.path, now=minutes(15))
        self.assertEqual((code, net.sent), (0, []))
        self.assertTrue(self.state()["1"]["up"])

    def write_state(self, data):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as fh:
            if isinstance(data, str):
                fh.write(data)
            else:
                json.dump(data, fh)

    def test_reminder_send_failure_keeps_previous_last_alert(self):
        prev = {"up": False, "since": T0.isoformat(), "last_alert": T0.isoformat(), "alerted": True}
        self.write_state({"1": prev})
        code, _ = run_main(FakeNet([DB_DOWN], telegram_ok=False), self.path, now=minutes(60))
        self.assertEqual(code, 1)
        self.assertEqual(self.state()["1"], prev)

    def test_up_send_failure_keeps_down_state(self):
        prev = {"up": False, "since": T0.isoformat(), "last_alert": T0.isoformat(), "alerted": True}
        self.write_state({"1": prev})
        code, _ = run_main(FakeNet([OK], telegram_ok=False), self.path, now=minutes(30))
        self.assertEqual(code, 1)
        self.assertEqual(self.state()["1"], prev)

    def test_send_failure_for_a_does_not_block_b_state(self):
        calls = {"n": 0}

        def send(token, chat_id, text):
            calls["n"] += 1
            return calls["n"] != 1  # tin đầu (target A) thất bại, tin sau thành công

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = canh.main([self.path], {
                "TARGETS": f"A|{FAKE_URL}\nB|https://example.org/h",
                "TELEGRAM_BOT_TOKEN": FAKE_TOKEN, "TELEGRAM_CHAT_ID": "987654321"},
                fetch_fn=FakeNet([DB_DOWN]).fetch, send_fn=send,
                sleep_fn=lambda s: None, now_fn=lambda: T0)
        self.assertEqual(code, 1)
        st = self.state()
        self.assertFalse(st["1"]["alerted"])
        self.assertTrue(st["2"]["alerted"])

    def test_state_saved_after_each_target(self):
        snapshots = []

        def fetch(url, timeout=20):
            if url.endswith("/h"):  # target B: xem file state tại thời điểm này
                snapshots.append(set(self.state()) if os.path.exists(self.path) else set())
            return DB_DOWN

        net = FakeNet([DB_DOWN])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            canh.main([self.path], {
                "TARGETS": f"A|{FAKE_URL}\nB|https://example.org/h",
                "TELEGRAM_BOT_TOKEN": FAKE_TOKEN, "TELEGRAM_CHAT_ID": "987654321"},
                fetch_fn=fetch, send_fn=net.send, sleep_fn=lambda s: None, now_fn=lambda: T0)
        self.assertEqual(snapshots[0], {"1"})  # A đã được lưu trước khi kiểm tra B

    def test_save_is_atomic_no_tmp_left(self):
        run_main(FakeNet([OK]), self.path)
        self.assertEqual(os.listdir(os.path.dirname(self.path)), ["state.json"])

    def test_missing_state_file_logged(self):
        _, out = run_main(FakeNet([OK]), self.path)
        self.assertIn("không có state trước (lần đầu hoặc cache hết hạn)", out)

    def test_unreadable_state_file_treated_as_empty(self):
        for junk in ("{khong phai json", "[1, 2]", "[" * 100000):
            with self.subTest(junk=junk[:10]):
                self.write_state(junk)
                net = FakeNet([DB_DOWN])
                code, out = run_main(net, self.path)
                self.assertEqual(code, 0)
                self.assertIn("state hỏng hoặc không đọc được", out)
                self.assertEqual(len(net.sent), 1)  # DOWN vẫn báo như lần đầu

    def test_invalid_entry_up_target_silent_down_target_alerts(self):
        bad_entries = [
            "chuoi", None, [], {"up": "yes", "since": T0.isoformat(), "last_alert": None},
            {"up": False, "since": "hom qua", "last_alert": None},
            {"up": False, "since": T0.isoformat(), "last_alert": 123},
            {"up": False, "since": "2026-01-01T03:00:00", "last_alert": None},  # thiếu múi giờ
            {"up": False, "since": T0.isoformat(), "last_alert": None, "alerted": "x"},
            {"up": 1, "since": T0.isoformat(), "last_alert": None},
        ]
        for bad in bad_entries:
            with self.subTest(bad=bad):
                self.write_state({"1": bad})
                net = FakeNet([OK])
                code, out = run_main(net, self.path, now=minutes(500))
                self.assertEqual((code, net.sent), (0, []))
                self.assertTrue(self.state()["1"]["up"])
                if bad is not None:
                    self.assertIn("state #1 hỏng, coi như lần đầu", out)
                self.write_state({"1": bad})
                net = FakeNet([DB_DOWN])
                code, _ = run_main(net, self.path, now=minutes(500))
                self.assertEqual((code, len(net.sent)), (0, 1))

    def test_valid_old_entry_without_alerted_still_works(self):
        self.write_state({"1": {"up": False, "since": T0.isoformat(), "last_alert": T0.isoformat()}})
        net = FakeNet([OK])
        run_main(net, self.path, now=minutes(30))
        self.assertIn("đã hoạt động lại", net.sent[0])

    def test_missing_secret_exit_2_clear_message(self):
        out = io.StringIO()
        with contextlib.redirect_stderr(out):
            code = canh.main([self.path], {"TARGETS": f"A|{FAKE_URL}"})
        self.assertEqual(code, 2)
        self.assertIn("TELEGRAM_BOT_TOKEN", out.getvalue())
        self.assertNotIn(FAKE_URL, out.getvalue())

    def test_bad_targets_exit_2(self):
        code, out = run_main(FakeNet([OK]), self.path, targets="hong")
        self.assertEqual(code, 2)
        self.assertNotIn("hong\n", out)

    def test_stdout_never_has_url_or_token(self):
        net = FakeNet([OSError(FAKE_URL + FAKE_TOKEN)])
        code, out = run_main(net, self.path, extra_env={"THU_BAO_DONG": "true"})
        for secret in (FAKE_URL, "example.com", FAKE_TOKEN, "Dịch vụ A", "987654321"):
            self.assertNotIn(secret, out)

    def test_removed_target_state_pruned(self):
        run_main(FakeNet([DB_DOWN]), self.path,
                 targets=f"A|{FAKE_URL}\nB|https://example.org/h")
        self.assertEqual(set(self.state()), {"1", "2"})
        run_main(FakeNet([OK]), self.path, targets=f"A|{FAKE_URL}")
        self.assertEqual(set(self.state()), {"1"})


class TestMode(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "state.json")

    def test_synthetic_target_alerts_and_not_persisted(self):
        net = FakeNet([OK, (404, b"nf")])
        code, out = run_main(net, self.path, extra_env={"THU_BAO_DONG": "true"})
        self.assertEqual(code, 0)
        self.assertEqual(net.calls[0], FAKE_URL)
        self.assertEqual(net.calls[1], FAKE_URL + "-thu-loi")
        self.assertEqual(len(net.sent), 2)  # 🔴 thử + ghi chú kết thúc (target thật UP, im lặng)
        self.assertIn("Dịch vụ A (THỬ báo động)", net.sent[0])
        self.assertIn("HTTP 404", net.sent[0])
        self.assertIn("Kết thúc thử", net.sent[1])
        self.assertEqual(list(self.state_keys()), ["1"])  # không có state cho target thử

    def state_keys(self):
        with open(self.path, encoding="utf-8") as fh:
            return json.load(fh).keys()

    def test_derive_url_strips_query_and_trailing_slash(self):
        self.assertEqual(canh.derive_test_url("https://example.com/api/health/?a=1"),
                         "https://example.com/api/health-thu-loi")

    def test_test_alert_failure_exit_1(self):
        net = FakeNet([OK, (404, b"")], telegram_ok=False)
        code, _ = run_main(net, self.path, extra_env={"THU_BAO_DONG": "true"})
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
