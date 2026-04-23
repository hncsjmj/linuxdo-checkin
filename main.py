"""
cron: 0 */6 * * *
new Env("Linux.Do 签到")
"""

import os
import random
import time
import functools
import json
from loguru import logger
from DrissionPage import ChromiumOptions, Chromium
from tabulate import tabulate
from curl_cffi import requests
from bs4 import BeautifulSoup
from notify import NotificationManager


def retry_decorator(retries=3, min_delay=5, max_delay=10):
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    if attempt == retries - 1:
                        logger.error(f"函数 {func.__name__} 最终执行失败: {str(e)}")
                    logger.warning(
                        f"函数 {func.__name__} 第 {attempt + 1}/{retries} 次尝试失败: {str(e)}"
                    )
                    if attempt < retries - 1:
                        sleep_s = random.uniform(min_delay, max_delay)
                        logger.info(
                            f"将在 {sleep_s:.2f}s 后重试 ({min_delay}-{max_delay}s 随机延迟)"
                        )
                        time.sleep(sleep_s)
            return None
        return wrapper
    return decorator


os.environ.pop("DISPLAY", None)
os.environ.pop("DYLD_LIBRARY_PATH", None)

USERNAME = os.environ.get("LINUXDO_USERNAME")
PASSWORD = os.environ.get("LINUXDO_PASSWORD")
COOKIES = os.environ.get("LINUXDO_COOKIES", "").strip()
BROWSE_ENABLED = os.environ.get("BROWSE_ENABLED", "true").strip().lower() not in [
    "false",
    "0",
    "off",
]
if not USERNAME:
    USERNAME = os.environ.get("USERNAME")
if not PASSWORD:
    PASSWORD = os.environ.get("PASSWORD")

HOME_URL = "https://linux.do/"
LOGIN_URL = "https://linux.do/login"
SESSION_URL = "https://linux.do/session"
CSRF_URL = "https://linux.do/session/csrf"


class LinuxDoBrowser:
    def __init__(self) -> None:
        from sys import platform

        if platform == "linux" or platform == "linux2":
            platformIdentifier = "X11; Linux x86_64"
        elif platform == "darwin":
            platformIdentifier = "Macintosh; Intel Mac OS X 10_15_7"
        elif platform == "win32":
            platformIdentifier = "Windows NT 10.0; Win64; x64"
        else:
            platformIdentifier = "X11; Linux x86_64"

        co = (
            ChromiumOptions()
            .headless(True)
            .incognito(True)
            .set_argument("--no-sandbox")
            .set_argument("--disable-gpu")
            .set_argument("--disable-dev-shm-usage")
            .set_argument("--window-size=1920,1080")
            # 关键: 绕过 Cloudflare 的 headless 检测
            .set_argument("--disable-blink-features=AutomationControlled")
        )
        co.set_user_agent(
            f"Mozilla/5.0 ({platformIdentifier}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
        )
        self.browser = Chromium(co)
        self.page = self.browser.new_tab()

        # 注入反检测 JS: 覆盖 navigator.webdriver
        self.page.run_js_loaded("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        """)

        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Accept-Language": "zh-CN,zh;q=0.9",
            }
        )
        self.notifier = NotificationManager()
        # 保存从浏览器获取的 cookie，用于后续 API 请求
        self._dp_cookies = {}

    @staticmethod
    def parse_cookie_string(cookie_str: str) -> list[dict]:
        cookies = []
        for part in cookie_str.strip().split(";"):
            part = part.strip()
            if "=" in part:
                name, _, value = part.partition("=")
                cookies.append(
                    {
                        "name": name.strip(),
                        "value": value.strip(),
                        "domain": ".linux.do",
                        "path": "/",
                    }
                )
        return cookies

    def _sync_cookies_to_session(self):
        """从 DrissionPage 浏览器同步 cookie 到 curl_cffi session，用于后续 API 请求"""
        try:
            dp_cookies = self.page.cookies()
            self._dp_cookies = {}
            for ck in dp_cookies:
                name = ck.get("name", "")
                value = ck.get("value", "")
                if name:
                    self.session.cookies.set(name, value, domain="linux.do")
                    self._dp_cookies[name] = value
            logger.info(f"已同步 {len(self._dp_cookies)} 个 Cookie 到 session")
        except Exception as e:
            logger.warning(f"同步 Cookie 到 session 失败: {e}")

    def login_with_cookies(self, cookie_str: str) -> bool:
        """使用手动设置的 Cookie 直接登录"""
        logger.info("检测到手动 Cookie，尝试 Cookie 登录...")
        dp_cookies = self.parse_cookie_string(cookie_str)
        if not dp_cookies:
            logger.error("Cookie 解析失败或为空，无法使用 Cookie 登录")
            return False

        logger.info(f"成功解析 {len(dp_cookies)} 个 Cookie 条目")

        # 设置到 DrissionPage
        self.page.set.cookies(dp_cookies)

        # 也设置到 curl_cffi session
        for ck in dp_cookies:
            self.session.cookies.set(ck["name"], ck["value"], domain="linux.do")

        logger.info("Cookie 设置完成，导航至 linux.do...")
        self.page.get(HOME_URL)

        # 等待 Cloudflare Challenge 通过
        self._wait_for_cf_challenge(timeout=30)

        # 从浏览器同步最新的 cookie（CF 会追加新 cookie）
        self._sync_cookies_to_session()

        # 验证登录状态 - 多种方式
        return self._verify_login()

    def login(self):
        """账号密码登录 - 通过浏览器方式而非 API，避开 Cloudflare"""
        logger.info("开始账号密码登录（浏览器方式）")
        self.page.get(LOGIN_URL)

        # 等待 Cloudflare Challenge 通过
        self._wait_for_cf_challenge(timeout=30)

        try:
            # 等待登录表单出现
            login_field = self.page.ele("#login-account-name", timeout=10)
            password_field = self.page.ele("#login-account-password", timeout=10)

            if not login_field or not password_field:
                logger.error("未找到登录表单元素")
                return False

            login_field.input(USERNAME)
            time.sleep(random.uniform(0.5, 1.5))
            password_field.input(PASSWORD)
            time.sleep(random.uniform(0.5, 1.0))

            # 点击登录按钮
            login_btn = self.page.ele("@type=submit", timeout=5)
            if login_btn:
                login_btn.click()
            else:
                logger.error("未找到登录按钮")
                return False

            # 等待登录完成
            time.sleep(5)

            # 同步 cookie
            self._sync_cookies_to_session()

            return self._verify_login()
        except Exception as e:
            logger.error(f"浏览器登录失败: {e}")
            return False

    def _wait_for_cf_challenge(self, timeout=30):
        """等待 Cloudflare Challenge 页面通过"""
        logger.info("等待 Cloudflare 验证通过...")
        start = time.time()
        while time.time() - start < timeout:
            title = self.page.title or ""
            html = self.page.html or ""
            # CF challenge 页面通常标题包含 "Just a moment" 或有 challenge-platform 脚本
            if "just a moment" not in title.lower() and "challenge-platform" not in html:
                logger.info("Cloudflare 验证已通过")
                return True
            time.sleep(2)
        logger.warning(f"Cloudflare 验证等待超时（{timeout}s），继续尝试...")
        return False

    def _verify_login(self) -> bool:
        """多种方式验证登录状态"""
        time.sleep(3)
        try:
            # 方式1: 检查 current-user 元素
            user_ele = self.page.ele("@id=current-user", timeout=5)
            if user_ele:
                logger.info("Cookie 登录验证成功 (current-user)")
                return True
        except Exception:
            pass

        try:
            # 方式2: 检查页面中是否有 avatar
            if "avatar" in (self.page.html or ""):
                logger.info("Cookie 登录验证成功 (avatar)")
                return True
        except Exception:
            pass

        try:
            # 方式3: 通过 Discourse API 验证
            resp = self.session.get(
                "https://linux.do/session/current.json",
                headers={"Accept": "application/json"},
                impersonate="chrome136",
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("current_user"):
                    logger.info(f"Cookie 登录验证成功 (API: {data['current_user'].get('username', '')})")
                    return True
        except Exception:
            pass

        logger.error("登录验证失败，Cookie 可能已过期")
        return False

    def click_topic(self):
        topic_list = self.page.ele("@id=list-area").eles(".:title")
        if not topic_list:
            logger.error("未找到主题帖")
            return False
        sample_size = min(10, len(topic_list))
        logger.info(f"发现 {len(topic_list)} 个主题帖，随机选择{sample_size}个")
        for topic in random.sample(topic_list, sample_size):
            self.click_one_topic(topic.attr("href"))
        return True

    @retry_decorator()
    def click_one_topic(self, topic_url):
        if not topic_url:
            return
        new_page = self.browser.new_tab()
        try:
            new_page.get(topic_url)
            if random.random() < 0.3:
                self.click_like(new_page)
            self.browse_post(new_page)
        finally:
            try:
                new_page.close()
            except Exception:
                pass

    def browse_post(self, page):
        prev_url = None
        for _ in range(10):
            scroll_distance = random.randint(550, 650)
            logger.info(f"向下滚动 {scroll_distance} 像素...")
            page.run_js(f"window.scrollBy(0, {scroll_distance})")
            logger.info(f"已加载页面: {page.url}")

            if random.random() < 0.03:
                logger.success("随机退出浏览")
                break

            at_bottom = page.run_js(
                "window.scrollY + window.innerHeight >= document.body.scrollHeight"
            )
            current_url = page.url
            if current_url != prev_url:
                prev_url = current_url
            elif at_bottom and prev_url == current_url:
                logger.success("已到达页面底部，退出浏览")
                break

            wait_time = random.uniform(2, 4)
            logger.info(f"等待 {wait_time:.2f} 秒...")
            time.sleep(wait_time)

    def click_like(self, page):
        try:
            like_button = page.ele(".discourse-reactions-reaction-button")
            if like_button:
                logger.info("找到未点赞的帖子，准备点赞")
                like_button.click()
                logger.info("点赞成功")
                time.sleep(random.uniform(1, 2))
            else:
                logger.info("帖子可能已经点过赞了")
        except Exception as e:
            logger.error(f"点赞失败: {str(e)}")

    def print_connect_info(self):
        """获取 Connect 信息 - 优先使用浏览器方式"""
        logger.info("获取连接信息")

        # 方式1: 使用浏览器直接访问 connect.linux.do
        try:
            connect_page = self.browser.new_tab()
            connect_page.get("https://connect.linux.do/")
            self._wait_for_cf_challenge(timeout=15)
            time.sleep(3)

            connect_html = connect_page.html or ""
            if "challenge-platform" not in connect_html:
                soup = BeautifulSoup(connect_html, "html.parser")
                rows = soup.select("table tr")
                info = []
                for row in rows:
                    cells = row.select("td")
                    if len(cells) >= 3:
                        project = cells[0].text.strip()
                        current = cells[1].text.strip() if cells[1].text.strip() else "0"
                        requirement = cells[2].text.strip() if cells[2].text.strip() else "0"
                        info.append([project, current, requirement])

                if info:
                    logger.info("--------------Connect Info-----------------")
                    logger.info("\n" + tabulate(info, headers=["项目", "当前", "要求"], tablefmt="pretty"))
                else:
                    logger.warning("Connect Info 页面未找到表格数据")

                try:
                    connect_page.close()
                except Exception:
                    pass
                return
        except Exception as e:
            logger.warning(f"浏览器方式获取 Connect Info 失败: {e}")
            try:
                connect_page.close()
            except Exception:
                pass

        # 方式2: 回退到 curl_cffi
        try:
            headers = {
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            }
            resp = self.session.get(
                "https://connect.linux.do/", headers=headers, impersonate="chrome136"
            )
            soup = BeautifulSoup(resp.text, "html.parser")
            rows = soup.select("table tr")
            info = []
            for row in rows:
                cells = row.select("td")
                if len(cells) >= 3:
                    project = cells[0].text.strip()
                    current = cells[1].text.strip() if cells[1].text.strip() else "0"
                    requirement = cells[2].text.strip() if cells[2].text.strip() else "0"
                    info.append([project, current, requirement])

            if info:
                logger.info("--------------Connect Info-----------------")
                logger.info("\n" + tabulate(info, headers=["项目", "当前", "要求"], tablefmt="pretty"))
            else:
                logger.warning("Connect Info: 未能获取数据（可能被 Cloudflare 拦截）")
        except Exception as e:
            logger.error(f"获取 Connect Info 失败: {e}")

    def send_notifications(self, browse_enabled):
        status_msg = f"✅每日登录成功: {USERNAME or 'Cookie用户'}"
        if browse_enabled:
            status_msg += " + 浏览任务完成"
        self.notifier.send_all("LINUX DO", status_msg)

    def run(self):
        try:
            # 优先使用手动 Cookie 登录
            if COOKIES:
                login_res = self.login_with_cookies(COOKIES)
                if not login_res:
                    if USERNAME and PASSWORD:
                        logger.warning("Cookie 登录失败，尝试账号密码登录...")
                        login_res = self.login()
                    else:
                        logger.error("Cookie 登录失败，且未配置账号密码")
            else:
                login_res = self.login()

            if not login_res:
                logger.warning("登录验证失败")
                # 仍然尝试继续执行，有时验证不够准确

            if BROWSE_ENABLED:
                click_topic_res = self.click_topic()
                if not click_topic_res:
                    logger.error("点击主题失败，程序终止")
                    return
                logger.info("完成浏览任务")

            self.print_connect_info()
            self.send_notifications(BROWSE_ENABLED)
        finally:
            try:
                self.page.close()
            except Exception:
                pass
            try:
                self.browser.quit()
            except Exception:
                pass


if __name__ == "__main__":
    if not COOKIES and (not USERNAME or not PASSWORD):
        print("请设置 LINUXDO_COOKIES（Cookie 登录），或同时设置 USERNAME 和 PASSWORD（账号密码登录）")
        exit(1)
    browser = LinuxDoBrowser()
    browser.run()
