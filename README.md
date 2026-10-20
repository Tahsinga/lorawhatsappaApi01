# WhatsappAPI

The bot reads branch names and transfer history from the deployed Lora web API; it does not use its local sample stock tables for these lookups.

Configure the same randomly generated `WHATSAPP_STOCK_API_TOKEN` on both the Lora API web service and WhatsApp bot. Set `LORA_STOCK_API_URL` on the bot to `https://laxdomloraapi.onrender.com/api/whatsapp/stock/`. The WhatsApp bot branch menu is limited to COMPLEX, CROSS COUNTRY, DZOVANI, INVESTMENTS, Mini Market, PERFECT, SISK, The Place, VEES, WEST, YEUKAI, and ZBS. The web API uses `WHATSAPP_STOCK_TIME_ZONE` for daily sent-stock cutoffs and timestamps; it defaults to `Africa/Harare`.

Conversation: a user sends any text to start, chooses a listed branch by number or exact name, then chooses `1` for today's transfers, `2` to enter a date in `YYYY-MM-DD` format, or `3` to choose another branch. The report lists each product, quantity, send time, and transfer status, plus a daily total. Reply `MENU` to restart. A session expires after 5 minutes without a message; the bot notifies the user, and their next message starts a fresh session.
