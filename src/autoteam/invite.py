#!/usr/bin/env python3
import autoteam.display  # noqa: F401 — 自动设置虚拟显示器

"""
ChatGPT Team 自动邀请 + 注册工具

完整流程:
1. CloudMail 创建临时邮箱
2. ChatGPT API 发送 Team 邀请
3. CloudMail 收取邀请邮件，提取邀请链接
4. Playwright 打开邀请链接，注册 ChatGPT 账号
5. CloudMail 收取验证码邮件，自动填入
6. 完成注册并加入 workspace

用法:
    python invite.py
"""

import logging
import os
import re
import sys
import time

from autoteam.signup_profile import SignupProfile, generate_signup_profile

logger = logging.getLogger(__name__)

MAIL_TIMEOUT = int(os.environ.get("MAIL_TIMEOUT", "180"))
SCREENSHOT_DIR = "screenshots"
REGISTRATION_ERROR_ATTR = "_autoteam_registration_error"


def _set_registration_error(page, error_type: str, detail: str | None = None):
    error = {"type": error_type, "detail": detail or error_type}
    try:
        setattr(page, REGISTRATION_ERROR_ATTR, error)
    except Exception:
        pass
    return False


def get_registration_error(page) -> dict | None:
    try:
        error = getattr(page, REGISTRATION_ERROR_ATTR, None)
    except Exception:
        return None
    return error if isinstance(error, dict) else None


def screenshot(page, name):
    os.makedirs(SCREENSHOT_DIR, exist_ok=True)
    path = f"{SCREENSHOT_DIR}/{name}"
    page.screenshot(path=path, full_page=True)
    logger.debug("[截图] %s", path)


def find_and_click(page, selectors, label="元素", timeout=3000):
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=timeout):
                logger.debug("[注册] 找到%s: %s", label, sel)
                loc.click()
                return True
        except Exception:
            continue
    return False


def find_visible(page, selectors, label="元素", timeout=3000):
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=timeout):
                logger.debug("[注册] 找到%s: %s", label, sel)
                return loc
        except Exception:
            continue
    return None


def _locator_text(locator) -> str:
    try:
        return str(locator.inner_text(timeout=800) or "")
    except Exception:
        return ""


def wait_for_cloudflare(page, max_wait=60):
    for i in range(max_wait // 5):
        html = page.content()[:2000].lower()
        if "verify you are human" not in html and "challenge" not in page.url:
            return True
        logger.info("[注册] 等待 Cloudflare... (%ds)", i * 5)
        time.sleep(5)
    return False


def _page_text(page, limit=4000) -> str:
    try:
        return page.inner_text("body", timeout=1000)[:limit]
    except Exception:
        try:
            return page.content()[:limit]
        except Exception:
            return ""


def _is_google_signin_page(page) -> bool:
    try:
        url = str(page.url or "").lower()
    except Exception:
        url = ""
    if "accounts.google." in url or "google.com/signin" in url or "google.com/o/oauth" in url:
        return True

    text = _page_text(page).lower()
    if "sign in with google" in text and ("email or phone" in text or "forgot email" in text) and "openai" in text:
        return True
    if "email or phone" in text and "forgot email" in text and "openai" in text:
        return True

    try:
        google_identifier = page.locator('#identifierId, input[name="identifier"]').first
        return bool(google_identifier.is_visible(timeout=800))
    except Exception:
        return False


def _abort_if_google_signin(page, screenshot_name: str) -> bool:
    if not _is_google_signin_page(page):
        return False
    logger.warning("[注册] 页面跳到了 Google sign-in；CFMail 账号不能走 Google OAuth，本次注册中止")
    screenshot(page, screenshot_name)
    _set_registration_error(page, "google_signin_redirect", "页面进入 Google sign-in，而不是 ChatGPT 邮箱注册/验证流程")
    return True


def _page_has_session_expired(page) -> bool:
    text = _page_text(page, limit=3000).lower()
    return "your session has expired" in text or "session has expired" in text


def _abort_if_session_expired(page, screenshot_name: str) -> bool:
    if not _page_has_session_expired(page):
        return False
    logger.warning("[注册] 页面提示 session 已过期，提前中止本次注册")
    screenshot(page, screenshot_name)
    _set_registration_error(page, "session_expired", "邀请链接页面提示 session 已过期")
    return True


def _recover_session_expired_once(page, invite_link: str, screenshot_name: str) -> bool:
    """邀请链接偶尔会先进 session-expired 页；点一次 Log in 继续真实登录流。"""
    if not _page_has_session_expired(page):
        return True

    logger.warning("[注册] 页面提示 session 已过期，尝试点击 Log in 恢复一次")
    screenshot(page, screenshot_name)
    clicked = find_and_click(
        page,
        [
            'button:has-text("Log in")',
            'a:has-text("Log in")',
            'button:has-text("登录")',
            'a:has-text("登录")',
        ],
        "session 过期登录按钮",
        timeout=5000,
    )
    if clicked:
        time.sleep(5)
        wait_for_cloudflare(page)
        screenshot(page, screenshot_name.replace(".png", "_after_login.png"))
        if not _page_has_session_expired(page):
            return True

    logger.info("[注册] Log in 未恢复，重新打开邀请链接再试一次")
    page.goto(invite_link, wait_until="domcontentloaded", timeout=60000)
    time.sleep(5)
    wait_for_cloudflare(page)
    screenshot(page, screenshot_name.replace(".png", "_after_reload.png"))
    if not _page_has_session_expired(page):
        return True

    logger.warning("[注册] session 过期恢复失败，本次注册中止")
    return False


def _birthday_text_values(signup_profile: SignupProfile) -> list[str]:
    return [
        f"{signup_profile.birth_month_text}/{signup_profile.birth_day_text}/{signup_profile.birth_year_text}",
        f"{signup_profile.birth_year_text}-{signup_profile.birth_month_text}-{signup_profile.birth_day_text}",
        signup_profile.birthday_text,
    ]


def _page_has_birthday_error(page) -> bool:
    try:
        text = page.inner_text("body", timeout=1000).lower()
    except Exception:
        return False
    return "doesn't look right" in text or "try again" in text or "生日" in text and "错误" in text


def _click_complete_account(page) -> bool:
    return find_and_click(
        page,
        [
            'button:has-text("完成帐户创建")',
            'button:has-text("Finish creating account")',
            'button:has-text("Complete")',
            'button:has-text("Continue")',
            'button:has-text("Agree")',
            'button[type="submit"]',
        ],
        "完成按钮",
    )


def _find_email_input(page, timeout=3000):
    if _is_google_signin_page(page):
        return None
    return find_visible(
        page,
        [
            'input[name="email"]',
            'input[type="email"]:not([name="identifier"])',
            'input[placeholder*="email" i]',
            'input[id="email"]',
            "#email-input",
            'input[autocomplete="email"]',
            'input[autocomplete="username"]:not([name="identifier"])',
        ],
        "邮箱输入框",
        timeout=timeout,
    )


def _has_password_input(page) -> bool:
    return bool(
        find_visible(
            page,
            [
                'input[name="password"]',
                'input[type="password"]',
                'input[id="password"]',
            ],
            "密码输入框",
            timeout=800,
        )
    )


def _has_verification_input(page) -> bool:
    try:
        if len(page.locator('input[maxlength="1"]').all()) >= 4:
            return True
    except Exception:
        pass
    return bool(
        find_visible(
            page,
            [
                'input[name="code"]',
                'input[placeholder*="code" i]',
                'input[placeholder*="验证" i]',
                'input[type="text"][inputmode="numeric"]',
                'input[inputmode="numeric"]',
            ],
            "验证码输入框",
            timeout=800,
        )
    )


def _still_on_email_entry_step(page) -> bool:
    if _is_google_signin_page(page):
        return True
    if _has_password_input(page) or _has_verification_input(page):
        return False
    return bool(_find_email_input(page, timeout=800))


def _submit_email_step(page, email: str) -> bool:
    if _abort_if_google_signin(page, "reg_03_google_signin_before_email.png"):
        return False

    email_input = _find_email_input(page)
    if not email_input:
        logger.info("[注册] 未找到邮箱输入框，可能页面已自动填入")
        screenshot(page, "reg_03_no_email_input.png")
        if _abort_if_session_expired(page, "reg_03_session_expired.png"):
            return False
        if _abort_if_google_signin(page, "reg_03_google_signin_no_email_input.png"):
            return False
        return True

    for attempt in range(1, 3):
        logger.info("[注册] 输入邮箱: %s (attempt %d/2)", email, attempt)
        try:
            email_input.fill(email)
        except Exception:
            email_input.click(force=True)
            page.keyboard.press("ControlOrMeta+A")
            page.keyboard.type(email, delay=40)
        time.sleep(1)

        clicked = _click_email_continue_button(page)
        if not clicked:
            try:
                page.keyboard.press("Enter")
            except Exception:
                pass
        time.sleep(5)
        screenshot(page, "reg_03_after_email.png" if attempt == 1 else f"reg_03_after_email_retry_{attempt}.png")

        if _abort_if_session_expired(page, "reg_03_session_expired.png"):
            return False
        if _abort_if_google_signin(page, "reg_03_google_signin_after_email.png"):
            return False
        if not _still_on_email_entry_step(page):
            return True

        logger.warning("[注册] 邮箱提交后仍停留在邮箱输入页，准备重试一次")
        email_input = _find_email_input(page, timeout=1500)
        if not email_input:
            return True

    _set_registration_error(page, "email_submit_failed", "邮箱提交后仍停留在邮箱输入页，未进入密码/验证码步骤")
    logger.warning("[注册] 邮箱提交失败，未进入密码/验证码步骤")
    return False


def _click_email_continue_button(page) -> bool:
    """Click the email-form Continue button without selecting social login providers."""
    blocked_words = ("google", "apple", "phone", "microsoft", "sso")
    selectors = [
        'button[type="submit"]',
        'button:has-text("Continue")',
        'button:has-text("继续")',
    ]
    for selector in selectors:
        try:
            buttons = page.locator(selector).all()
        except Exception:
            buttons = []
        for button in buttons:
            try:
                if not button.is_visible(timeout=800):
                    continue
                text = _locator_text(button).lower()
                if any(word in text for word in blocked_words):
                    logger.debug("[注册] 跳过社交登录按钮: %s", text)
                    continue
                button.click()
                return True
            except Exception:
                continue
    return False


def _find_birthday_text_input(page):
    try:
        loc = page.get_by_label(re.compile(r"^(birthday|date of birth|生日日期|出生日期)$", re.I)).first
        if loc.is_visible(timeout=1500):
            return loc
    except Exception:
        pass
    return find_visible(
        page,
        [
            'input[name*="birth" i]',
            'input[id*="birth" i]',
            'input[aria-label*="birthday" i]',
            'input[aria-label*="date of birth" i]',
            'input[placeholder*="MM" i]',
            'input[placeholder*="YYYY" i]',
        ],
        "生日输入框",
        timeout=1500,
    )


def _fill_birthday_text_input(page, birthday_input, signup_profile: SignupProfile) -> bool:
    for value in _birthday_text_values(signup_profile):
        try:
            birthday_input.click(force=True)
            time.sleep(0.2)
            try:
                birthday_input.fill("")
            except Exception:
                page.keyboard.press("ControlOrMeta+A")
                time.sleep(0.1)
            page.keyboard.type(value, delay=70)
            logger.info("[注册] 已填入随机生日: %s (text input)", value)
            _click_complete_account(page)
            time.sleep(5)
            if not _page_has_birthday_error(page):
                screenshot(page, "reg_07_after_profile.png")
                return True
            logger.info("[注册] 生日格式未被接受，尝试下一个格式")
        except Exception as exc:
            logger.debug("[注册] 生日文本框填写失败: %s", exc)
    return False


def _complete_invite_about_you(page, signup_profile: SignupProfile) -> bool:
    name_input = find_visible(
        page,
        [
            'input[name="name"]',
            'input[placeholder*="name" i]',
            'input[id="name"]',
            'input[placeholder*="全名" i]',
        ],
        "名字输入框",
        timeout=5000,
    )

    if name_input:
        name_input.fill(signup_profile.full_name)
        logger.info("[注册] 已填入随机姓名: %s", signup_profile.full_name)
        time.sleep(0.5)

    birthday_input = _find_birthday_text_input(page)
    if birthday_input and _fill_birthday_text_input(page, birthday_input, signup_profile):
        return True

    filled_age = False
    spinbuttons = page.locator('[role="spinbutton"]').all()
    if len(spinbuttons) >= 3:
        try:
            page.locator("text=生日日期").click()
            time.sleep(0.5)
        except Exception:
            pass
        for sb, val in zip(spinbuttons[:3], signup_profile.positional_birthday_orders()[0]):
            sb.click(force=True)
            time.sleep(0.2)
            page.keyboard.type(val, delay=80)
            time.sleep(0.3)
        logger.info("[注册] 已填入随机生日: %s (spinbutton)", signup_profile.birthday_text)
        filled_age = True
    else:
        age_input = find_visible(
            page,
            [
                'input[name="age"]',
                'input[id="age"]',
                'input[placeholder*="age" i]',
                'input[placeholder*="年龄" i]',
                'input[type="number"]',
            ],
            "年龄输入框",
            timeout=3000,
        )
        if age_input:
            age_input.fill(signup_profile.age_text)
            logger.info("[注册] 已填入随机年龄: %s", signup_profile.age_text)
            filled_age = True

    if not (name_input or filled_age):
        return False

    _click_complete_account(page)
    time.sleep(8)
    screenshot(page, "reg_07_after_profile.png")
    return True


def register_with_invite(
    page, invite_link, email, mail_client, password=None, signup_profile: SignupProfile | None = None
):
    """用邀请链接注册 ChatGPT 账号并加入 workspace，返回 (success, password)"""
    signup_profile = signup_profile or generate_signup_profile()

    logger.info("[注册] 打开邀请链接...")
    page.goto(invite_link, wait_until="domcontentloaded", timeout=60000)
    time.sleep(5)
    wait_for_cloudflare(page)
    screenshot(page, "reg_01_invite_page.png")
    logger.info("[注册] 当前 URL: %s", page.url)
    if not _recover_session_expired_once(page, invite_link, "reg_01_session_expired.png"):
        _set_registration_error(page, "session_expired", "session expired 恢复失败")
        return False, password
    if _abort_if_google_signin(page, "reg_01_google_signin.png"):
        return False, password

    # 可能需要点击 Sign up
    find_and_click(
        page,
        [
            'button:has-text("Sign up")',
            'a:has-text("Sign up")',
            'button:has-text("Create account")',
            'a:has-text("Create account")',
            'button:has-text("注册")',
        ],
        "注册按钮",
        timeout=5000,
    )
    time.sleep(3)
    screenshot(page, "reg_02_signup.png")
    if not _recover_session_expired_once(page, invite_link, "reg_02_session_expired.png"):
        _set_registration_error(page, "session_expired", "点击注册后 session expired 恢复失败")
        return False, password
    if _abort_if_google_signin(page, "reg_02_google_signin.png"):
        return False, password

    # 输入邮箱
    if not _submit_email_step(page, email):
        return False, password

    # 可能需要输入密码（注册流程）
    pwd_input = find_visible(
        page,
        [
            'input[name="password"]',
            'input[type="password"]',
            'input[id="password"]',
        ],
        "密码输入框",
        timeout=5000,
    )

    if pwd_input:
        if not password:
            import uuid

            password = f"Tmp_{uuid.uuid4().hex[:12]}!"
        logger.info("[注册] 设置密码: [redacted]")
        pwd_input.fill(password)
        time.sleep(1)

        find_and_click(
            page,
            [
                'button:has-text("Continue")',
                'button:has-text("继续")',
                'button[type="submit"]',
            ],
            "继续按钮",
        )
        time.sleep(5)
        screenshot(page, "reg_04_after_password.png")
        if _abort_if_session_expired(page, "reg_04_session_expired.png"):
            return False, password
        if _abort_if_google_signin(page, "reg_04_google_signin.png"):
            return False, password

    if _abort_if_google_signin(page, "reg_05_google_signin_before_code_wait.png"):
        return False, password

    # 等待验证码邮件
    logger.info("[注册] 等待 ChatGPT 发送验证码到 %s...", email)
    verification_code = None
    try:
        # 搜索来自 OpenAI 的验证码邮件（不是邀请邮件）
        start = time.time()
        while time.time() - start < MAIL_TIMEOUT:
            if _abort_if_session_expired(page, "reg_05_session_expired.png"):
                return False, password
            emails = mail_client.search_emails_by_recipient(email, size=10)
            for em in emails:
                subject = em.get("subject", "").lower()
                sender = em.get("sendEmail", "").lower()
                # 跳过邀请邮件，只要验证码邮件
                if "invited" in subject or "invitation" in subject:
                    continue
                if "openai" in sender or "chatgpt" in sender:
                    verification_code = mail_client.extract_verification_code(em)
                    if verification_code:
                        logger.info("[CloudMail] 收到验证码: [redacted]")
                        break
            if verification_code:
                break
            elapsed = int(time.time() - start)
            print(f"\r[CloudMail] 等待验证码... ({elapsed}s)", end="", flush=True)
            time.sleep(3)
    except Exception as e:
        logger.error("[注册] 等待验证码异常: %s", e)

    if not verification_code:
        logger.warning("[注册] 未自动获取到验证码")
        screenshot(page, "reg_05_no_code.png")
        _set_registration_error(page, "verification_mail_missing", "未收到 OpenAI/ChatGPT 验证码邮件")
        return False, password

    # 输入验证码
    logger.info("[注册] 输入验证码: [redacted]")
    screenshot(page, "reg_05_before_code.png")

    # 检查是否是多个单字符输入框
    single_inputs = page.locator('input[maxlength="1"]').all()
    if len(single_inputs) >= 4:
        logger.debug("[注册] 检测到 %d 个单字符输入框", len(single_inputs))
        for i, char in enumerate(verification_code):
            if i < len(single_inputs):
                single_inputs[i].fill(char)
                time.sleep(0.2)
    else:
        code_input = find_visible(
            page,
            [
                'input[name="code"]',
                'input[placeholder*="code" i]',
                'input[placeholder*="验证" i]',
                'input[type="text"]',
                'input[inputmode="numeric"]',
            ],
            "验证码输入框",
        )
        if code_input:
            code_input.fill(verification_code)
        else:
            logger.warning("[注册] 未找到验证码输入框")
            screenshot(page, "reg_05_no_code_input.png")
            _set_registration_error(page, "verification_input_missing", "收到验证码后页面未出现可填写的验证码输入框")
            return False, password

    time.sleep(1)

    # 点击确认
    find_and_click(
        page,
        [
            'button:has-text("Continue")',
            'button:has-text("Verify")',
            'button:has-text("Submit")',
            'button[type="submit"]',
        ],
        "确认按钮",
    )

    time.sleep(8)
    screenshot(page, "reg_06_after_code.png")
    logger.info("[注册] 当前 URL: %s", page.url)

    _complete_invite_about_you(page, signup_profile)

    # 可能需要接受条款 / 加入 workspace
    find_and_click(
        page,
        [
            'button:has-text("Accept")',
            'button:has-text("Agree")',
            'button:has-text("Join")',
            'button:has-text("Join workspace")',
            'button:has-text("加入")',
            'button:has-text("Accept invite")',
        ],
        "加入/接受按钮",
        timeout=5000,
    )
    time.sleep(5)
    screenshot(page, "reg_08_final.png")

    # 检查结果
    current_url = page.url
    page_text = page.inner_text("body")[:500].lower()

    if "chatgpt.com" in current_url and "auth" not in current_url:
        logger.info("[注册] 注册成功并已加入 workspace!")
        return True, password
    elif "workspace" in page_text or "welcome" in page_text:
        logger.info("[注册] 已加入 workspace!")
        return True, password
    else:
        logger.warning("[注册] 注册流程可能未完成，请查看截图")
        _set_registration_error(page, "workspace_join_incomplete", f"最终 URL={current_url}，未确认进入 workspace")
        return False, password


def run():
    """兼容旧入口：不创建新 invite，只消费已有 pending invite。"""
    from autoteam.manager import cmd_add

    return bool((cmd_add() or {}).get("invited"))


def main():
    logger.info("ChatGPT Team pending invite 兜底消费工具")
    result = run()
    sys.exit(0 if result else 1)


if __name__ == "__main__":
    main()
