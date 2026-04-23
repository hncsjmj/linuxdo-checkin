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
            .set_argument("--disable-blink-features=AutomationControlled")
        )
        co.set_user_agent(
            f"Mozilla/5.0 ({platformIdentifier}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
        )
        self.browser = Chromium(co)
        self.page = self.browser.new_tab()

        # 注入反检测 JS
        self.page.run_js_loaded("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            window.chrome = {runtime: {}};
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

    def _is_cf_challenge_page(self):
        """判断当前页面是否在 Cloudflare 挑战页
        关键：不能只检查 'challenge-platform' 是否在 HTML 中，
        因为 Discourse 正常页面也可能包含该字符串。
        真正的 CF 挑战页特征：标题包含 'just a moment'，或页面极短且包含特定 CF 标识。
        """
        try:
            title = (self.page.title or "").lower()
            html = self.page.html or ""

            # CF 经典挑战页标题
            if "just a moment" in title:
                return True
            if "checking your browser" in title:
                return True
            if "please wait" in title and len(html) < 5000:
                return True

            # 短页面 + CF 特征 = 还在挑战
            if len(html) < 5000:
                if "challenge-platform" in html or "cf-browser-verification" in html:
                    return True
                if "enable javascript" in html.lower() and "cloudflare" in html.lower():
                    return True

            return False
        except Exception:
            return True  # 出错时保守判断为 CF 页

    def _wait_for_cf_challenge(self, timeout=60):
        """等待 Cloudflare Challenge 页面通过"""
        logger.info("等待 Cloudflare 验证通过...")
        start = time.time()
        while time.time() - start < timeout:
            if not self._is_cf_challenge_page():
                elapsed = time.time() - start
                logger.info(f"Cloudflare 验证已通过 (耗时 {elapsed:.1f}s)")
                return True

            logger.debug(f"仍在 CF 挑战页，继续等待... ({time.time()-start:.0f}s)")
            time.sleep(3)

        logger.warning(f"Cloudflare 验证等待超时（{timeout}s）")
        # 打印调试信息
        try:
            logger.debug(f"当前页面标题: {self.page.title}")
            logger.debug(f"当前 HTML 长度: {len(self.page.html or '')}")
        except Exception:
            pass
        return False

    def _wait_for_page_ready(self, timeout=20):
        """等待页面实质性内容加载完成"""
        start = time.time()
        while time.time() - start < timeout:
            try:
                html = self.page.html or ""
                # Discourse 论坛页面加载后会有这些特征
                if len(html) > 100000 and "discourse" in html.lower():
                    logger.info("页面已完全加载")
                    return True
            except Exception:
                pass
            time.sleep(2)
        logger.warning("页面加载等待超时")
        return False

    def login_with_cookies(self, cookie_str: str) -> bool:
        """使用手动设置的 Cookie 直接登录"""
        logger.info("检测到手动 Cookie，尝试 Cookie 登录...")
        dp_cookies = self.parse_cookie_string(cookie_str)
        if not dp_cookies:
            logger.error("Cookie 解析失败或为空，无法使用 Cookie 登录")
            return False

        logger.info(f"成功解析 {len(dp_cookies)} 个 Cookie 条目")

        # 第1步：先导航到 linux.do，让浏览器自行通过 CF 挑战，获取 cf_clearance
        logger.info("先导航至 linux.do 建立 CF cookie...")
        self.page.get(HOME_URL)

        # 等待 CF 挑战通过（首次访问可能需要较长时间）
        cf_passed = self._wait_for_cf_challenge(timeout=60)
        if not cf_passed:
            logger.error("首次访问未能通过 Cloudflare")
            return False

        # 等待页面完全加载
        self._wait_for_page_ready(timeout=20)
        time.sleep(3)

        # 第2步：设置用户 Cookie
        logger.info("设置用户 Cookie...")
        self.page.set.cookies(dp_cookies)
        for ck in dp_cookies:
            self.session.cookies.set(ck["name"], ck["value"], domain="linux.do")

        # 第3步：刷新页面让 Cookie 生效
        logger.info("Cookie 设置完成，刷新页面...")
        self.page.get(HOME_URL)
        self._wait_for_cf_challenge(timeout=30)
        self._wait_for_page_ready(timeout=20)
        time.sleep(3)

        # 同步浏览器 Cookie 到 session
        self._sync_cookies_to_session()

        return self._verify_login()

    def login(self):
        """账号密码登录 - 通过浏览器方式"""
        logger.info("开始账号密码登录（浏览器方式）")
        self.page.get(LOGIN_URL)
        self._wait_for_cf_challenge(timeout=60)
        self._wait_for_page_ready(timeout=20)

        # Discourse 需要先点击登录按钮弹出模态框
        try:
            # 尝试找登录按钮
            login_btn = None
            for selector in ['.login-button', '.btn.btn-primary.btn-small.login-button',
                           'button.login-button', 'a.login-button']:
                try:
                    login_btn = self.page.ele(selector, timeout=3)
                    if login_btn:
                        logger.info(f"找到登录按钮: {selector}")
                        login_btn.click()
                        time.sleep(3)
                        break
                except Exception:
                    continue

            # 尝试找表单（可能已经在模态框中）
            login_field = self.page.ele("#login-account-name", timeout=5)
            if not login_field:
                # 尝试通过文本点击
                try:
                    header_login = self.page.ele("text:Log In", timeout=5) or self.page.ele("text:登录", timeout=5)
                    if header_login:
                        header_login.click()
                        time.sleep(3)
                except Exception:
                    pass
                login_field = self.page.ele("#login-account-name", timeout=5)

            if not login_field:
                logger.error("未找到登录表单元素")
                logger.debug(f"当前 URL: {self.page.url}")
                logger.debug(f"当前标题: {self.page.title}")
                return False

            password_field = self.page.ele("#login-account-password", timeout=5)

            login_field.input(USERNAME)
            time.sleep(random.uniform(0.5, 1.5))
            password_field.input(PASSWORD)
            time.sleep(random.uniform(0.5, 1.0))

            submit_btn = self.page.ele("#login-button", timeout=5)
            if not submit_btn:
                submit_btn = self.page.ele("button.btn-primary", timeout=3)
            if submit_btn:
                submit_btn.click()
            else:
                logger.error("未找到提交按钮")
                return False

            time.sleep(8)
            self._sync_cookies_to_session()
            return self._verify_login()
        except Exception as e:
            logger.error(f"浏览器登录失败: {e}")
            return False

    def _verify_login(self) -> bool:
        """多种方式验证登录状态"""
        time.sleep(5)

        # 方式1: 检查 current-user 元素（最可靠）
        try:
            user_ele = self.page.ele("@id=current-user", timeout=10)
            if user_ele:
                logger.info("登录验证成功 (current-user 元素)")
                return True
        except Exception:
            pass

        # 方式2: 检查页面中是否有 current-user 相关内容
        try:
            html = self.page.html or ""
            if 'id="current-user"' in html or "current-user" in html and "avatar" in html:
                logger.info("登录验证成功 (HTML 特征)")
                return True
        except Exception:
            pass

        # 方式3: 通过 Discourse API 验证
        try:
            self._sync_cookies_to_session()
            resp = self.session.get(
                "https://linux.do/session/current.json",
                headers={
                    "Accept": "application/json",
                    "X-Requested-With": "XMLHttpRequest",
                },
                impersonate="chrome136",
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("current_user"):
                    logger.info(f"登录验证成功 (API: {data['current_user'].get('username', '')})")
                    return True
            else:
                logger.debug(f"API 验证返回状态码: {resp.status_code}")
        except Exception as e:
            logger.debug(f"API 验证异常: {e}")

        # 方式4: 检查用户菜单按钮
        try:
            user_menu = self.page.ele(".header-dropdown-toggle.current-user", timeout=3)
            if user_menu:
                logger.info("登录验证成功 (用户菜单)")
                return True
        except Exception:
            pass

        # 调试信息
        try:
            logger.debug(f"验证时 URL: {self.page.url}")
            logger.debug(f"验证时标题: {self.page.title}")
            html = self.page.html or ""
            if self._is_cf_challenge_page():
                logger.error("页面仍在 Cloudflare 挑战页")
            elif len(html) < 10000:
                logger.error(f"页面内容过短 ({len(html)} 字符)")
                logger.debug(f"页面内容: {html[:500]}")
            else:
                logger.debug(f"HTML 长度: {len(html)}")
                logger.debug(f"包含 current-user: {'current-user' in html}")
                logger.debug(f"包含 avatar: {'avatar' in html}")
        except Exception:
            pass

        logger.error("登录验证失败，Cookie 可能已过期")
        return False

    def click_topic(self):
        # 确保在首页
        if self.page.url != HOME_URL:
            self.page.get(HOME_URL)
            self._wait_for_page_ready(timeout=15)

        try:
            list_area = self.page.ele("@id=list-area", timeout=15)
        except Exception:
            logger.error("未找到帖子列表区域 (#list-area)")
            return False

        if not list_area:
            logger.error("帖子列表区域为空")
            return False

        topic_list = list_area.eles(".:title")
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
            time.sleep(3)
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
        """获取 Connect 信息"""
        logger.info("获取连接信息")

        try:
            connect_page = self.browser.new_tab()
            connect_page.get("https://connect.linux.do/")

            # 等待页面加载（可能也有 CF）
            start = time.time()
            while time.time() - start < 20:
                try:
                    html = connect_page.html or ""
                    # 非挑战页且有实质内容
                    if len(html) > 1000 and "just a moment" not in (connect_page.title or "").lower():
                        break
                except Exception:
                    pass
                time.sleep(3)

            time.sleep(3)
            connect_html = connect_page.html or ""
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
                logger.warning("Connect Info: 未找到表格数据")

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

        # 回退到 curl_cffi
        try:
            headers = {
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
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
                logger.warning("Connect Info: 未能获取数据")
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
            else:
                # 确保页面在首页
                if self.page.url != HOME_URL:
                    self.page.get(HOME_URL)
                    self._wait_for_page_ready(timeout=15)

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
