import json
import os
import smtplib
import ssl
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

API_URL = "https://www.hascodexratelimitreset.today/api/status"
STATE_FILE = Path.cwd() / ".github" / "state" / "codex-reset-state.json"
SMTP_HOST = "smtp.qq.com"
SMTP_PORT = 465

QQ_MAIL_USER = os.environ.get("QQ_MAIL_USER", "").strip()
QQ_MAIL_AUTH_CODE = os.environ.get("QQ_MAIL_AUTH_CODE", "").strip()
MAIL_TO = os.environ.get("MAIL_TO", "").strip() or QQ_MAIL_USER
FORCE_NOTIFY = os.environ.get("FORCE_NOTIFY", "false").lower() == "true"


def read_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {
            "lastObservedState": None,
            "lastNotifiedResetAt": None,
            "consecutiveFailures": 0,
            "failureNotificationSent": False,
        }


def write_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def fetch_status(max_attempts: int = 3) -> dict:
    last_error: Exception | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            request = urllib.request.Request(
                API_URL,
                headers={
                    "Cache-Control": "no-cache",
                    "User-Agent": "codex-reset-monitor/1.0",
                    "Accept": "application/json",
                },
            )
            with urllib.request.urlopen(request, timeout=20) as response:
                if response.status != 200:
                    raise RuntimeError(f"状态接口返回 HTTP {response.status}")
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError, RuntimeError) as exc:
            last_error = exc
            print(f"第 {attempt}/{max_attempts} 次状态查询失败：{exc}")
            if attempt < max_attempts:
                time.sleep(5 * attempt)

    raise RuntimeError(f"状态接口连续 {max_attempts} 次查询失败：{last_error}")


def send_email(subject: str, body: str) -> None:
    if not QQ_MAIL_USER:
        raise RuntimeError("缺少 GitHub Secret：QQ_MAIL_USER")
    if not QQ_MAIL_AUTH_CODE:
        raise RuntimeError("缺少 GitHub Secret：QQ_MAIL_AUTH_CODE")
    if not MAIL_TO:
        raise RuntimeError("缺少收件邮箱地址")

    message = EmailMessage()
    message["From"] = QQ_MAIL_USER
    message["To"] = MAIL_TO
    message["Subject"] = subject
    message.set_content(body, charset="utf-8")

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=context, timeout=30) as smtp:
        smtp.login(QQ_MAIL_USER, QQ_MAIL_AUTH_CODE)
        smtp.send_message(message)

    print(f"中文邮件已成功发送至：{MAIL_TO}")


def status_details(status: dict) -> tuple[str, str, str, str]:
    current_state = str(status.get("state") or "unknown")
    reset_at = str(status.get("resetAt") or "未知")
    summary = status.get("automationSummary") or {}
    source_url = summary.get("tweetUrl") or "https://www.hascodexratelimitreset.today/"
    rationale = summary.get("rationale") or "未提供具体判断原因"
    return current_state, reset_at, rationale, source_url


def build_status_body(status: dict, is_test: bool = False) -> str:
    current_state, reset_at, rationale, source_url = status_details(status)
    checked_at = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")

    intro = (
        "这是一封手动触发的中文测试邮件，用于确认 GitHub Actions 已能通过 QQ 邮箱正常发送通知。"
        if is_test
        else "监控程序检测到 Codex 额度状态可能已从未刷新变为已刷新。"
    )

    return "\n".join(
        [
            intro,
            "",
            f"当前状态：{current_state}",
            f"额度刷新时间：{reset_at}",
            f"检测时间：{checked_at}",
            f"判断原因：{rationale}",
            f"信息来源：{source_url}",
            "",
            "说明：该结果来自第三方社区监控网站，并非 OpenAI 官方的个人账户额度查询结果。",
        ]
    )


def main() -> None:
    state = read_state()

    try:
        status = fetch_status()
    except Exception as exc:
        state["consecutiveFailures"] = int(state.get("consecutiveFailures", 0)) + 1
        failure_count = state["consecutiveFailures"]

        if failure_count >= 3 and not state.get("failureNotificationSent", False):
            try:
                send_email(
                    "【Codex监控异常】状态接口连续查询失败",
                    "\n".join(
                        [
                            "Codex 额度监控程序连续多次无法读取第三方状态接口。",
                            "",
                            f"连续失败次数：{failure_count}",
                            f"最后错误：{exc}",
                            f"状态接口：{API_URL}",
                            "",
                            "这通常表示第三方网站暂时故障，不代表你的 Codex 账户或 QQ 邮箱有问题。",
                        ]
                    ),
                )
                state["failureNotificationSent"] = True
            except Exception as mail_exc:
                print(f"监控异常邮件发送失败：{mail_exc}")

        write_state(state)
        raise

    state["consecutiveFailures"] = 0
    state["failureNotificationSent"] = False

    current_state, current_reset_at, _, _ = status_details(status)
    previous_state = state.get("lastObservedState")
    transitioned_to_yes = (
        previous_state == "no"
        and current_state == "yes"
        and current_reset_at != state.get("lastNotifiedResetAt")
    )

    print(f"上一次状态：{previous_state}")
    print(f"当前状态：{current_state}")
    print(f"当前刷新时间：{current_reset_at}")
    print(f"是否强制发送测试邮件：{FORCE_NOTIFY}")

    if FORCE_NOTIFY:
        send_email(
            "【Codex额度提醒测试】QQ邮箱通知配置成功",
            build_status_body(status, is_test=True),
        )

    if transitioned_to_yes:
        send_email(
            "【Codex额度提醒】Codex额度可能已刷新",
            build_status_body(status, is_test=False),
        )
        state["lastNotifiedResetAt"] = current_reset_at
        print("检测到 no → yes 状态变化，已发送中文邮件提醒。")

    state["lastObservedState"] = current_state
    write_state(state)


if __name__ == "__main__":
    main()
