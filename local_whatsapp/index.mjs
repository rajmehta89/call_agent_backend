import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import dotenv from "dotenv";
import qrcode from "qrcode-terminal";
import whatsappWeb from "whatsapp-web.js";

const { Client, LocalAuth } = whatsappWeb;
const serviceDir = path.dirname(fileURLToPath(import.meta.url));
dotenv.config({ path: path.resolve(serviceDir, "..", ".env") });

const backendUrl = (process.env.LOCAL_WHATSAPP_BACKEND_URL || "http://127.0.0.1:8000").replace(/\/$/, "");
const healthPort = Number(process.env.LOCAL_WHATSAPP_PORT || 8787);
const allowGroups = ["1", "true", "yes"].includes(String(process.env.LOCAL_WHATSAPP_ALLOW_GROUPS || "").toLowerCase());
const serviceToken = process.env.LOCAL_WHATSAPP_SERVICE_TOKEN || "";
const chromePath = process.env.LOCAL_WHATSAPP_CHROME_PATH || "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";

let state = "starting";
let lastError = null;
let connectedNumber = null;

function json(res, statusCode, value) {
  res.writeHead(statusCode, { "content-type": "application/json; charset=utf-8" });
  res.end(JSON.stringify(value));
}

const healthServer = http.createServer((req, res) => {
  if (req.url === "/health" || req.url === "/") {
    json(res, 200, {
      service: "local_whatsapp_web",
      state,
      connected_number: connectedNumber,
      backend_url: backendUrl,
      last_error: lastError,
    });
    return;
  }
  json(res, 404, { error: "not_found" });
});

healthServer.listen(healthPort, "127.0.0.1", () => {
  console.log(`Local WhatsApp health: http://127.0.0.1:${healthPort}/health`);
});

const client = new Client({
  authStrategy: new LocalAuth({ dataPath: path.join(serviceDir, ".wwebjs_auth") }),
  puppeteer: {
    headless: true,
    ...(fs.existsSync(chromePath) ? { executablePath: chromePath } : {}),
    args: ["--no-sandbox", "--disable-setuid-sandbox"],
  },
});

client.on("qr", (qr) => {
  state = "waiting_for_qr";
  lastError = null;
  console.log("\nScan this QR code with WhatsApp > Linked devices > Link a device:\n");
  qrcode.generate(qr, { small: true });
});

client.on("authenticated", () => {
  state = "authenticated";
  lastError = null;
  console.log("WhatsApp Web authenticated.");
});

client.on("ready", () => {
  state = "ready";
  lastError = null;
  connectedNumber = client.info?.wid?.user ? `+${client.info.wid.user}` : null;
  console.log(`WhatsApp Web ready${connectedNumber ? ` as ${connectedNumber}` : ""}.`);
});

client.on("auth_failure", (message) => {
  state = "auth_failure";
  lastError = String(message);
  console.error(`WhatsApp authentication failed: ${lastError}`);
});

client.on("disconnected", (reason) => {
  state = "disconnected";
  lastError = String(reason);
  console.error(`WhatsApp Web disconnected: ${lastError}`);
});

client.on("message", async (message) => {
  if (message.fromMe || message.from === "status@broadcast") return;
  if (!allowGroups && message.from.endsWith("@g.us")) return;

  const text = String(message.body || "").trim();
  if (!text) return;

  const rawPhone = message.from.split("@")[0];
  const customerPhone = /^\d+$/.test(rawPhone) ? `+${rawPhone}` : rawPhone;
  let customerName = "";
  try {
    const contact = await message.getContact();
    customerName = contact.pushname || contact.name || "";
  } catch {
    // The phone number is enough for the shared brain and inbox.
  }

  console.log(`Inbound WhatsApp message from ${customerPhone}: ${text}`);
  try {
    const headers = { "content-type": "application/json" };
    if (serviceToken) headers["x-local-whatsapp-token"] = serviceToken;
    const response = await fetch(`${backendUrl}/api/whatsapp/local/message`, {
      method: "POST",
      headers,
      body: JSON.stringify({ customer_phone: customerPhone, customer_name: customerName, text }),
    });
    const payload = await response.json();
    if (!response.ok || !payload.success) {
      throw new Error(payload.error || payload.detail?.error || `Backend returned ${response.status}`);
    }
    const reply = String(payload.data?.reply || "").trim();
    if (reply) {
      await message.reply(reply);
      console.log(`AI reply sent to ${customerPhone}: ${reply}`);
    } else {
      console.log("No AI reply was generated; inbox/handoff handling remains active.");
    }
  } catch (error) {
    lastError = error instanceof Error ? error.message : String(error);
    console.error(`Local WhatsApp message handling failed: ${lastError}`);
  }
});

async function shutdown(signal) {
  console.log(`\n${signal}: closing local WhatsApp service...`);
  healthServer.close();
  await client.destroy().catch(() => {});
  process.exit(0);
}

process.on("SIGINT", () => shutdown("SIGINT"));
process.on("SIGTERM", () => shutdown("SIGTERM"));

console.log(`Connecting local WhatsApp Web to ${backendUrl}`);
client.initialize().catch((error) => {
  state = "initialization_failed";
  lastError = error instanceof Error ? error.message : String(error);
  console.error(`WhatsApp Web initialization failed: ${lastError}`);
  process.exitCode = 1;
});
