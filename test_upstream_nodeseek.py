"""回归验证上游 NodeSeek 修复与本 fork 的兼容性。"""
import contextlib
import io
import unittest
from unittest import mock

from test_daily import daily
from bs4 import BeautifulSoup


def account_panel(text):
    return (
        '<aside id="nsk-right-panel-container"><div class="user-card">'
        '<a class="Username" href="/space/1">test-account</a>'
        f'<div class="user-stat">{text}</div></div></aside>'
    )


class Browser:
    def __init__(self, home_text="", board_text="", reject_domain=False):
        self.home_text = home_text
        self.board_text = board_text
        self.reject_domain = reject_domain
        self.current_url = "https://www.nodeseek.com"
        self.title = "NodeSeek"
        self.capabilities = {"browserVersion": "153.0.8010.52"}
        self.cookies = []
        self.closed = False

    @property
    def page_source(self):
        return self.board_text if self.current_url.endswith("/board") else self.home_text

    def get(self, url):
        self.current_url = url

    def add_cookie(self, cookie):
        if self.reject_domain and "domain" in cookie:
            raise ValueError("invalid cookie domain")
        self.cookies.append(cookie)

    def refresh(self):
        pass

    def execute_script(self, *args):
        pass

    def quit(self):
        self.closed = True


class MissingButtonWait:
    def __init__(self, *args, **kwargs):
        pass

    def until(self, *args, **kwargs):
        raise TimeoutError("领取按钮不存在")


class BalanceRefreshWait:
    def __init__(self, driver, *args, **kwargs):
        self.driver = driver

    def until(self, *args, **kwargs):
        return mock.Mock(click=lambda: setattr(self.driver, "home_text", account_panel("等级 Lv 1 鸡腿 123")))


class CookieWrappersTestCase(unittest.TestCase):
    def test_paired_quotes_preserve_cookie_names_and_values(self):
        for raw in ('"session=private-token; pjwt=xyz"', "'session=private-token; pjwt=xyz'"):
            with self.subTest(raw=raw):
                pairs, skipped = daily.parse_cookie_string(raw)
                self.assertEqual(pairs, [("session", "private-token"), ("pjwt", "xyz")])
                self.assertEqual(skipped, [])

    def test_header_prefixes_preserve_login_cookie(self):
        for prefix in ("Cookie: ", "Set-Cookie: ", "COOKIE: "):
            with self.subTest(prefix=prefix):
                pairs, skipped = daily.parse_cookie_string(prefix + "session=private-token; pjwt=xyz")
                self.assertEqual(pairs, [("session", "private-token"), ("pjwt", "xyz")])
                self.assertEqual(skipped, [])

    def test_curl_wrappers_preserve_login_cookie(self):
        for raw in (
            "-H 'Cookie: session=private-token; pjwt=xyz'",
            '--header="Cookie: session=private-token; pjwt=xyz"',
            '--cookie "session=private-token; pjwt=xyz"',
            "-b session=private-token; pjwt=xyz",
        ):
            with self.subTest(raw=raw):
                pairs, skipped = daily.parse_cookie_string(raw)
                self.assertEqual(pairs, [("session", "private-token"), ("pjwt", "xyz")])
                self.assertEqual(skipped, [])

    def test_invisible_paste_characters_do_not_drop_login_cookie(self):
        for invisible in ("\ufeff", "\u200b", "\u200c", "\u200d"):
            with self.subTest(invisible=invisible):
                pairs, skipped = daily.parse_cookie_string(f'{invisible}"session=private-token; pjwt=xyz"')
                self.assertEqual(pairs, [("session", "private-token"), ("pjwt", "xyz")])
                self.assertEqual(skipped, [])

    def test_semicolons_and_equals_in_values_are_preserved(self):
        pairs, skipped = daily.parse_cookie_string('"session=a==;b;c; pjwt=xyz"')
        self.assertEqual(pairs, [("session", "a==;b;c"), ("pjwt", "xyz")])
        self.assertEqual(skipped, [])

    def test_malformed_fragment_diagnostics_never_contain_credentials(self):
        _, skipped = daily.parse_cookie_string("私密说明:private-token; pjwt=xyz")
        output = " ".join(skipped)
        self.assertIn("重新复制", output)
        self.assertIn("首个非法字符", output)
        self.assertNotIn("private-token", output)
        self.assertNotIn("私密说明", output)


class CookieInjectionTestCase(unittest.TestCase):
    FRAGMENT = "0123456789abcdef0123456789abcdef"

    def inject(self, raw, reject_domain=False):
        driver = Browser(reject_domain=reject_domain)
        site = daily.Site("NodeSeek", "nodeseek.com", raw)
        output = io.StringIO()
        with mock.patch.object(daily, "wait_for_cloudflare", return_value=True), \
                mock.patch.object(daily.time, "sleep"), contextlib.redirect_stdout(output):
            result = daily.inject_site_cookies(driver, site)
        return result, driver, output.getvalue()

    def test_cookies_use_current_host_when_explicit_domain_is_rejected(self):
        result, driver, _ = self.inject("session=private-token; pjwt=xyz", reject_domain=True)
        self.assertTrue(result)
        self.assertEqual(driver.cookies, [
            {"name": "session", "value": "private-token", "path": "/"},
            {"name": "pjwt", "value": "xyz", "path": "/"},
        ])

    def test_truncated_login_candidate_is_injected_without_logging_value(self):
        result, driver, output = self.inject(f"{self.FRAGMENT}; cf_clearance=x; pjwt=xyz")
        self.assertTrue(result)
        cookies = {cookie["name"]: cookie["value"] for cookie in driver.cookies}
        self.assertEqual(cookies.get("session"), self.FRAGMENT)
        self.assertIn("试注入", output)
        self.assertNotIn(self.FRAGMENT, output)

    def test_existing_session_is_not_overwritten_by_orphan_fragment(self):
        result, driver, output = self.inject(f"{self.FRAGMENT}; session=private-token; pjwt=xyz")
        self.assertTrue(result)
        sessions = [cookie["value"] for cookie in driver.cookies if cookie["name"] == "session"]
        self.assertEqual(sessions, ["private-token"])
        self.assertNotIn("试注入", output)
        self.assertNotIn(self.FRAGMENT, output)

    def test_short_or_whitespace_fragments_are_not_injected_as_session(self):
        for fragment in ("abc", "garbage text", "站点说明 abc"):
            with self.subTest(fragment=fragment):
                result, driver, _ = self.inject(f"{fragment}; pjwt=xyz")
                self.assertTrue(result)
                self.assertEqual([cookie["name"] for cookie in driver.cookies], ["pjwt"])

    def test_missing_session_reports_request_header_copy_guidance(self):
        result, _, output = self.inject("pjwt=xyz; smac=1")
        self.assertTrue(result)
        self.assertIn("HttpOnly", output)
        self.assertIn("请求头", output)

    def test_redirected_or_insecure_hosts_never_receive_cookies(self):
        for url in (
            "https://untrusted.example/",
            "https://www.nodeseek.com.untrusted.example/",
            "http://www.nodeseek.com/",
            "https://www.nodeseek.com@untrusted.example/",
        ):
            with self.subTest(url=url):
                driver = Browser()
                driver.get = lambda requested_url: setattr(driver, "current_url", url)
                site = daily.Site("NodeSeek", "nodeseek.com", "session=private-token")
                with mock.patch.object(daily, "wait_for_cloudflare", return_value=True), \
                        mock.patch.object(daily.time, "sleep"):
                    self.assertFalse(daily.inject_site_cookies(driver, site))
                self.assertEqual(driver.cookies, [])


class SignedEvidenceTestCase(unittest.TestCase):
    def detect(self, text):
        driver = Browser(board_text=text)
        driver.get("https://www.nodeseek.com/board")
        with mock.patch.object(daily, "BeautifulSoup", BeautifulSoup):
            return daily.detect_already_signed(driver)

    def test_generic_signin_navigation_is_not_completion(self):
        for text in ("今日签到", "已签到", "已经签到", "今日已签到", "欢迎回来，今日签到"):
            with self.subTest(text=text):
                self.assertFalse(self.detect(text))

    def test_completed_reward_sentences_confirm_signin(self):
        for text in ("今日签到获得鸡腿3个", "签到成功，获得 5 个鸡腿", "签到奖励鸡腿 10 个"):
            with self.subTest(text=text):
                self.assertTrue(self.detect(text))

    def test_next_day_message_confirms_signin(self):
        for text in ("今日奖励已领取，请明天再来", "明日再来"):
            with self.subTest(text=text):
                self.assertTrue(self.detect(text))

    def test_promotional_future_rewards_are_not_completion(self):
        for text in ("每日签到可获得 5 个鸡腿", "签到即可领取鸡腿5个", "签到将奖励鸡腿 10 个"):
            with self.subTest(text=text):
                self.assertFalse(self.detect(text))


class LoginVerifiedRunTestCase(unittest.TestCase):
    def run_site(self, home_text, board_text, cookie="session=private-token", wait=MissingButtonWait):
        driver = Browser(home_text=home_text, board_text=board_text)
        site = daily.Site("NodeSeek", "nodeseek.com", cookie)
        output = io.StringIO()
        with mock.patch.object(daily, "load_sites", return_value=[site]), \
                mock.patch.object(daily, "create_driver", return_value=driver), \
                mock.patch.object(daily, "wait_for_cloudflare", return_value=True), \
                mock.patch.object(daily, "BeautifulSoup", BeautifulSoup), \
                mock.patch.object(daily, "WebDriverWait", wait), \
                mock.patch.object(daily.EC, "element_to_be_clickable", return_value=object(), create=True), \
                mock.patch.object(daily.time, "sleep"), \
                mock.patch.object(daily, "extra_tasks_enabled", False), \
                mock.patch.object(daily.notify, "send") as send, contextlib.redirect_stdout(output):
            code = daily.run()
        self.assertTrue(driver.closed)
        return code, send.call_args.args[1], output.getvalue()

    def test_unverified_login_cannot_accept_completed_signin_text(self):
        for cookie in ("session=private-token", "pjwt=xyz"):
            with self.subTest(cookie=cookie):
                code, notification, _ = self.run_site("欢迎访问", "明天再来", cookie=cookie)
                self.assertEqual(code, 1)
                self.assertIn("未确认登录态", notification)
                self.assertIn("账号概览: 未抓到", notification)

    def test_verified_login_accepts_completed_reward_text(self):
        code, notification, _ = self.run_site(account_panel("等级 Lv 1 鸡腿 118"), "签到成功，获得 5 个鸡腿")
        self.assertEqual(code, 0)
        self.assertIn("今日已签到", notification)
        self.assertIn("总鸡腿数: 118", notification)

    def test_verified_login_still_rejects_generic_navigation(self):
        code, notification, _ = self.run_site(account_panel("等级 Lv 1 鸡腿 118"), "今日签到")
        self.assertEqual(code, 1)
        self.assertIn("未找到领取按钮", notification)

    def test_account_summary_is_checked_before_signin(self):
        events = []
        site = daily.Site("NodeSeek", "nodeseek.com", "session=private-token")

        def summary(driver, current_site):
            events.append("summary")
            return {"level": "1"}

        def signin(driver, current_site, **kwargs):
            events.append("signin")
            self.assertTrue(kwargs.get("logged_in"))
            return {"success": True, "detail": "今日已签到"}

        with mock.patch.object(daily, "load_sites", return_value=[site]), \
                mock.patch.object(daily, "create_driver", return_value=Browser()), \
                mock.patch.object(daily, "inject_site_cookies", return_value=True), \
                mock.patch.object(daily, "fetch_account_summary", side_effect=summary), \
                mock.patch.object(daily, "click_sign_icon", side_effect=signin), \
                mock.patch.object(daily, "extra_tasks_enabled", False), \
                mock.patch.object(daily.notify, "send"):
            self.assertEqual(daily.run(), 0)
        self.assertEqual(events[:2], ["summary", "signin"])

    def test_notification_uses_post_signin_balance(self):
        code, notification, _ = self.run_site(
            account_panel("等级 Lv 1 鸡腿 118"), "签到成功，获得 5 个鸡腿", wait=BalanceRefreshWait)
        self.assertEqual(code, 0)
        self.assertIn("总鸡腿数: 123", notification)
        self.assertNotIn("总鸡腿数: 118", notification)

    def test_unverified_login_after_click_is_not_reported_as_success(self):
        button = mock.Mock()
        wait = mock.Mock(return_value=mock.Mock(until=mock.Mock(return_value=button)))
        code, notification, _ = self.run_site("欢迎访问", "明天再来", wait=wait)
        self.assertEqual(code, 1)
        self.assertIn("登录态未确认", notification)

    def test_failed_orphan_recovery_remains_failure_without_leaking_token(self):
        fragment = CookieInjectionTestCase.FRAGMENT
        code, notification, output = self.run_site("欢迎访问", "明天再来", cookie=f"{fragment}; pjwt=xyz")
        self.assertEqual(code, 1)
        self.assertIn("未确认登录态", notification)
        self.assertIn("试注入仍无效", output)
        self.assertNotIn(fragment, output)

    def test_verified_orphan_recovery_reports_confirmation(self):
        fragment = CookieInjectionTestCase.FRAGMENT
        code, notification, output = self.run_site(account_panel("等级 Lv 1 鸡腿 118"), "明天再来", cookie=f"{fragment}; pjwt=xyz")
        self.assertEqual(code, 0)
        self.assertIn("今日已签到", notification)
        self.assertIn("登录态已确认", output)
        self.assertNotIn(fragment, output)

    def test_public_post_statistics_do_not_confirm_login(self):
        code, notification, _ = self.run_site("<h2>公开文章：鸡腿 100</h2>", "明天再来")
        self.assertEqual(code, 1)
        self.assertIn("未确认登录态", notification)


class AccountSummaryScopeTestCase(unittest.TestCase):
    def summary(self, source):
        driver = Browser(home_text=source)
        site = daily.Site("NodeSeek", "nodeseek.com", "session=private-token")
        with mock.patch.object(daily, "BeautifulSoup", BeautifulSoup), \
                mock.patch.object(daily, "wait_for_cloudflare", return_value=True), \
                mock.patch.object(daily.time, "sleep"):
            return daily.fetch_account_summary(driver, site)

    def test_public_statistics_without_account_panel_are_ignored(self):
        self.assertEqual(self.summary("<h2>等级 Lv 99 鸡腿 999 评论数 1000</h2>"), {})

    def test_only_account_panel_statistics_are_returned(self):
        source = "<h2>等级 Lv 99 鸡腿 999</h2>" + account_panel("等级 Lv 1 鸡腿 118")
        self.assertEqual(self.summary(source), {"level": "1", "chicken_leg": "118"})

    def test_account_panel_without_identity_does_not_confirm_login(self):
        source = (
            '<aside id="nsk-right-panel-container"><div class="user-card">'
            '<div class="user-stat">等级 Lv 1 鸡腿 118</div></div></aside>'
        )
        self.assertEqual(self.summary(source), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
