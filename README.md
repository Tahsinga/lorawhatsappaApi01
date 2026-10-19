# WhatsappAPI

The bot reads branch names and transfer history from the deployed Lora web API; it does not use its local sample stock tables for these lookups.

Configure the same randomly generated `WHATSAPP_STOCK_API_TOKEN` on both the Lora API web service and WhatsApp bot. Set `LORA_STOCK_API_URL` on the bot to `https://laxdomloraapi.onrender.com/api/whatsapp/stock/`. The web API uses `WHATSAPP_STOCK_TIME_ZONE` for daily received-stock cutoffs and timestamps; it defaults to `Africa/Harare`.

Conversation: a user sends any text to start, chooses a listed branch by number or exact name, then replies `1` to see stock received today. The report lists completed receipts with product, quantity, and receipt time, plus a daily total. Reply `2` to choose another branch or `MENU` to restart.
