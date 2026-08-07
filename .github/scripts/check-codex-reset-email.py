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
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}

    return {
        "lastObservedState": state.get("lastObservedState"),
        "lastNotifiedResetAt": state.get("lastNotifiedResetAt"),
        "consecutiveFailures": int(state.get("consecutiveFailures", 0)),
        "failureNotificationSent": bool(state.get("failureNotificationSent", False)),
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
                    "User-Agent": "codex-reset-monitor/2.0",
                    "Accept": "application/json",
                },
            )
            with urllib.request.urlopen(request, timeout=20) as response:
                if response.status != 200:
                    raise RuntimeError(f"状态接口返回 HTTP {response.status}")
                return json.loads(response.read().decode("utf-8"))
        except (
            urllib.error.URLError,
            urllib.error.HTTPError,
            TimeoutError,
            json.JSONDecodeError,
            RuntimeError,
        ) as exc:
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


def build_status_body(status: dict) -> str:
    current_state, reset_at, rationale, source_url = status_details(status)
    checked_at = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")

    return "\n".join(
        [
            "监控程序检测到 Codex 额度状态可能已从未刷新变为已刷新。",
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


def send_test_email() -> None:
    checked_at = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    send_email(
        "【Codex额度提醒测试】QQ邮箱通知配置成功",
        "\n".join(
            [
                "这是一封手动触发的中文测试邮件。",
                "",
                "如果你能正常收到并阅读这封邮件，说明 GitHub Actions → QQ 邮箱的通知通道工作正常。",
                f"测试时间：{checked_at}",
                "",
                "测试邮件不依赖第三方 Codex 状态网站是否正常。",
            ]
        ),
    )


def handle_upstream_failure(state: dict, exc: Exception) -> None:
    # 上游第三方网站临时故障属于可预期情况，不应把整个 GitHub Actions 标记为失败。
    # 最多累计到 3；达到 3 后只发送一次中文异常提醒，避免每 10 分钟重复轰炸。
    if state.get("failureNotificationSent", False):
        state["consecutiveFailures"] = 3
        write_state(state)
        print("第三方状态接口仍不可用；此前已发送过异常提醒，本次不重复发送。")
        return

    failure_count = min(int(state.get("consecutiveFailures", 0)) + 1, 3)
    state["consecutiveFailures"] = failure_count

    print(f"第三方状态接口连续失败计数：{failure_count}/3")

    if failure_count >= 3:
        send_email(
            "【Codex监控异常】第三方状态网站连续查询失败",
            "\n".join(
                [
                    "Codex 额度监控程序已连续 3 轮无法读取第三方状态网站。",
                    "",
                    f"最后错误：{exc}",
                    f"状态接口：{API_URL}",
                    "",
                    "这通常表示第三方网站暂时故障，并不代表你的 Codex 账户或 QQ 邮箱出现问题。",
                    "后续监控仍会继续运行；网站恢复后，失败计数会自动清零。",
                ]
            ),
        )
        state["failureNotificationSent"] = True

    write_state(state)


def main() -> None:
    state = read_state()

    # 手动测试邮件与第三方状态接口解耦：即使状态网站挂了，也能单独验证邮箱通道。
    if FORCE_NOTIFY:
        send_test_email()

    try:
        status = fetch_status()
    except Exception as exc:
        handle_upstream_failure(state, exc)
        print("本轮因第三方状态网站不可用而结束，但监控程序本身运行正常。")
        return

    # 上游恢复后清除故障状态。
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

    if transitioned_to_yes:
        send_email(
            "【Codex额度提醒】Codex额度可能已刷新",
            build_status_body(status),
        )
        state["lastNotifiedResetAt"] = current_reset_at
        print("检测到 no → yes 状态变化，已发送中文邮件提醒。")

    state["lastObservedState"] = current_state
    write_state(state)


if __name__ == "__main__":
    main()
