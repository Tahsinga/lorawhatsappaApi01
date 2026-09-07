import json
import sqlite3
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.http import require_POST
from django.shortcuts import render


def _messages():
	if not settings.WHATSAPP_DB_PATH.exists():
		return []

	connection = sqlite3.connect(settings.WHATSAPP_DB_PATH)
	connection.row_factory = sqlite3.Row
	try:
		rows = connection.execute(
			"SELECT id, ts, wa_id, direction, message_type, body "
			"FROM conversations ORDER BY ts ASC, id ASC"
		).fetchall()
	except sqlite3.OperationalError:
		rows = []
	finally:
		connection.close()
	return [dict(row) for row in rows]


def inbox(request):
	return render(request, 'webui/inbox.html')


def messages_api(request):
	return JsonResponse({'messages': _messages()})


def _record_outgoing(wa_id, body):
	connection = sqlite3.connect(settings.WHATSAPP_DB_PATH)
	try:
		connection.execute(
			"INSERT INTO conversations(ts, wa_id, direction, message_type, body) "
			"VALUES (?, ?, 'outgoing', 'text', ?)",
			(int(time.time()), wa_id, body),
		)
		connection.commit()
	finally:
		connection.close()


@require_POST
def reply_api(request):
	try:
		payload = json.loads(request.body or '{}')
	except json.JSONDecodeError:
		return JsonResponse({'error': 'Request body must be valid JSON.'}, status=400)

	wa_id = str(payload.get('wa_id', '')).strip()
	body = str(payload.get('body', '')).strip()
	if not wa_id or not body:
		return JsonResponse({'error': 'A recipient and message are required.'}, status=400)
	if len(body) > 4096:
		return JsonResponse({'error': 'Messages must be 4096 characters or fewer.'}, status=400)
	if not settings.WA_PHONE_ID or not settings.WA_TOKEN:
		return JsonResponse({'error': 'WA_PHONE_ID or WA_TOKEN is missing from .env.'}, status=503)

	request_body = json.dumps({
		'messaging_product': 'whatsapp',
		'recipient_type': 'individual',
		'to': wa_id,
		'type': 'text',
		'text': {'preview_url': False, 'body': body},
	}).encode('utf-8')
	meta_request = Request(
		f'https://graph.facebook.com/v25.0/{settings.WA_PHONE_ID}/messages',
		data=request_body,
		headers={
			'Authorization': f'Bearer {settings.WA_TOKEN}',
			'Content-Type': 'application/json',
		},
		method='POST',
	)
	try:
		with urlopen(meta_request, timeout=20) as response:
			meta_response = json.loads(response.read().decode('utf-8'))
	except HTTPError as error:
		detail = error.read().decode('utf-8', errors='replace')
		return JsonResponse({'error': f'Meta rejected the message: {detail}'}, status=502)
	except (URLError, TimeoutError) as error:
		return JsonResponse({'error': f'Could not reach Meta: {error}'}, status=502)

	_record_outgoing(wa_id, body)
	return JsonResponse({'ok': True, 'message_id': meta_response.get('messages', [{}])[0].get('id')})

