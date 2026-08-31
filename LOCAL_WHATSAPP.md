# Local WhatsApp Web bridge

This development-only bridge uses a normal WhatsApp account through WhatsApp Web. It sends inbound messages to the existing `/api/whatsapp/local/message` route, so the same `BrainService` and WhatsApp inbox are used without replacing the official Meta Cloud API or Twilio WhatsApp paths.

Start the backend first, then run from this repository:

```powershell
.\start_local_whatsapp.ps1
```

Scan the terminal QR code from WhatsApp **Settings → Linked devices**. The bridge health endpoint is:

```text
http://127.0.0.1:8787/health
```

Set `LOCAL_WHATSAPP_BACKEND_URL` when the backend is not on `http://127.0.0.1:8000`. Groups are ignored by default; set `LOCAL_WHATSAPP_ALLOW_GROUPS=true` only for intentional testing.

This path is for local testing only. Use Meta Cloud API or Twilio WhatsApp with an approved sender for production delivery.
