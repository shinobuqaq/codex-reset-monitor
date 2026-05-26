import fs from "node:fs";
import path from "node:path";

const apiUrl = "https://www.hascodexratelimitreset.today/api/status";
const stateFile = path.join(process.cwd(), ".github", "state", "codex-reset-state.json");

const qmsgKey = process.env.QMSG_KEY;
const qmsgQQ = process.env.QMSG_QQ || "";
const forceNotify = String(process.env.FORCE_NOTIFY).toLowerCase() === "true";

function readState() {
  try {
    return JSON.parse(fs.readFileSync(stateFile, "utf8"));
  } catch {
    return {
      lastObservedState: null,
      lastNotifiedResetAt: null
    };
  }
}

function writeState(state) {
  fs.mkdirSync(path.dirname(stateFile), { recursive: true });
  fs.writeFileSync(stateFile, `${JSON.stringify(state, null, 2)}\n`, "utf8");
}

async function fetchStatus() {
  const response = await fetch(apiUrl, {
    headers: {
      "Cache-Control": "no-cache"
    }
  });

  if (!response.ok) {
    throw new Error(`Status request failed with ${response.status}`);
  }

  return response.json();
}

function buildMessage(status, isTest = false) {
  const currentState = String(status.state || "unknown");
  const resetAt = status.resetAt == null ? "unknown" : String(status.resetAt);
  const tweetUrl = status?.automationSummary?.tweetUrl || "https://www.hascodexratelimitreset.today/";
  const rationale = status?.automationSummary?.rationale || "";
  const prefix = isTest ? "【Codex额度提醒测试】" : "【Codex额度可能已刷新】";

  return [
    prefix,
    `状态：${currentState}`,
    `resetAt：${resetAt}`,
    rationale ? `原因：${rationale}` : "",
    `来源：${tweetUrl}`
  ].filter(Boolean).join("\n");
}

async function sendQmsg(message) {
  if (!qmsgKey) {
    throw new Error("Missing QMSG_KEY secret");
  }

  const response = await fetch(`https://qmsg.zendee.cn/jsend/${qmsgKey}`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json"
    },
    body: JSON.stringify({
      msg: message,
      ...(qmsgQQ ? { qq: qmsgQQ } : {})
    })
  });

  const body = await response.text();

  if (!response.ok) {
    throw new Error(`Qmsg request failed with ${response.status}: ${body}`);
  }

  let parsed;
  try {
    parsed = JSON.parse(body);
  } catch {
    throw new Error(`Qmsg returned non-JSON response: ${body}`);
  }

  if (!parsed.success) {
    throw new Error(`Qmsg push failed: ${body}`);
  }

  console.log(`Qmsg sent successfully: ${body}`);
}

const status = await fetchStatus();
const previousState = readState();

const currentState = String(status.state || "");
const currentResetAt = status.resetAt == null ? null : String(status.resetAt);

const transitionedToYes =
  previousState.lastObservedState === "no" &&
  currentState === "yes" &&
  currentResetAt !== previousState.lastNotifiedResetAt;

console.log(`Previous state: ${previousState.lastObservedState ?? "null"}`);
console.log(`Current state: ${currentState}`);
console.log(`Current resetAt: ${currentResetAt ?? "null"}`);
console.log(`Force notify: ${forceNotify}`);

if (forceNotify) {
  await sendQmsg(buildMessage(status, true));
  console.log("Sent Qmsg test message.");
}

if (transitionedToYes) {
  await sendQmsg(buildMessage(status, false));
  previousState.lastNotifiedResetAt = currentResetAt;
  console.log("Detected no->yes transition and sent Qmsg notification.");
}

previousState.lastObservedState = currentState;
writeState(previousState);
