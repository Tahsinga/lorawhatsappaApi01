# WhatsappAPI

The bot reads branch names and transfer history from the deployed Lora web API; it does not use its local sample stock tables for these lookups.

Configure the same randomly generated `WHATSAPP_STOCK_API_TOKEN` on both the Lora API web service and WhatsApp bot. Set `LORA_STOCK_API_URL` on the bot to `https://laxdomloraapi.onrender.com/api/whatsapp/stock/`. The WhatsApp bot branch menu is limited to COMPLEX, CROSS COUNTRY, DZOVANI, INVESTMENTS, Mini Market, PERFECT, RUJEKO B, SISK, The Place, VEES, WEST, YEUKAI, and ZBS. The web API uses `WHATSAPP_STOCK_TIME_ZONE` for daily received-stock cutoffs and timestamps; it defaults to `Africa/Harare`.

For public access to the WhatsApp Control Room through a custom domain or ngrok, set `WHATSAPP_DASHBOARD_HOST` in `.env` to the hostname only (for example, `appendix-exposure-purple.ngrok-free.dev`). The bot allows that exact host and its HTTPS origin for dashboard requests and replies. Render's hostname is detected automatically.

Conversation: a user sends any text to start, chooses a listed branch by number or exact name, then chooses `1` for stock received today, `2` to enter a date in `YYYY-MM-DD` format, or `3` to choose another branch. The report lists each received product, quantity, and receive time, plus a daily total. Reply `MENU` to restart. A session expires after 5 minutes without a message; the bot notifies the user, and their next message starts a fresh session.

## Deploy to Render

The repository includes a Render Blueprint (`render.yaml`) that runs the WhatsApp webhook/API and Django Control Room in the same web service. In Render, create a new Blueprint and select this repository. The service root directory must be the repository root (the Blueprint sets `rootDir: .`); do not set it to `config` or `dashboard/config`. The build command installs `requirements.txt` and runs Django static collection from the repository root, while the start command runs migrations and starts Uvicorn from that same root. Set the prompted secrets (`WA_PHONE_ID`, `WA_TOKEN`, `WA_VERIFY_TOKEN`, `MODEL_API_KEY`, and `WHATSAPP_STOCK_API_TOKEN`) in the Render dashboard. Set `WA_APP_SECRET` there as well to enable Meta webhook signature validation.

Render automatically configures the service hostname, runs database migrations when the service starts, and serves Django static files through WhiteNoise. The Blueprint includes a persistent disk mounted at `/var/data` for the conversation database, Django database, and received media; persistent disks require a paid Render instance. Configure the Meta webhook callback as `https://<your-render-hostname>/webhook` and use the same `WA_VERIFY_TOKEN` value in Meta. The dashboard is available at the service root (`/`) and at `/control-room/`; `/health` is the health check.

For local development, copy `.env.example` to `.env`, fill in the WhatsApp and model credentials, and start the service with:

```powershell
python -m uvicorn main:fastapi_app --host 127.0.0.1 --port 8000
```
