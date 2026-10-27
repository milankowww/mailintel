# SPDX-FileCopyrightText: Milan Pikula
# SPDX-License-Identifier: EUPL-1.2

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from mailintel.api.routers.dashboard import register_routes


def test_star_routes_validate_values_missing_messages_and_pagination():
    rows = {1: {"id": 1, "star": 0}}

    def save(message_id, star):
        if message_id not in rows:
            return None
        rows[message_id]["star"] = star
        return rows[message_id]

    repo = SimpleNamespace(
        set_message_star=save,
        list_message_stars=lambda: [row for row in rows.values() if row["star"] > 0],
        list_starred_messages=lambda limit, offset: [row for row in rows.values() if row["star"] > 0][offset:offset + limit],
    )
    app = FastAPI()
    register_routes(app, SimpleNamespace(repository=repo, dashboard_service=None, chat_service=None))
    client = TestClient(app)
    for invalid in [-1, 2147483648, True, 1.5, "1", None]:
        assert client.put('/api/messages/1/star', json={"star": invalid}).status_code == 422
    assert client.put('/api/messages/2/star', json={"star": 1}).status_code == 404
    assert client.put('/api/messages/1/star', json={"star": 2}).json() == {"id": 1, "star": 2}
    assert client.get('/api/message-stars').json() == {"stars": [{"id": 1, "star": 2}]}
    assert client.get('/api/starred?limit=1').json() == {"messages": [{"id": 1, "star": 2}], "has_more": False}
    assert client.get('/api/starred?offset=-1').status_code == 400
    assert client.get('/api/starred?limit=201').status_code == 400
    assert client.put('/api/messages/1/star', json={"star": 0}).status_code == 200
    assert client.get('/api/starred').json()["messages"] == []
    route = next(route for route in app.routes if route.path.endswith('/star'))
    assert route.endpoint.mailintel_capability == 'mail.write'


def test_message_detail_returns_sender_to_and_cc_with_summary():
    row = {'id': 7, 'sender_name': 'Sender', 'sender_address': 'sender@example.com',
           'recipients_json': '["Alice <alice@example.com>", "bob@example.com"]',
           'cc_json': ['carol@example.com'], 'summary_text': 'Review the report.'}
    repo = SimpleNamespace(get_message_detail=lambda message_id: row if message_id == 7 else None)
    app = FastAPI()
    register_routes(app, SimpleNamespace(repository=repo, dashboard_service=None, chat_service=None))
    client = TestClient(app)
    response = client.get('/api/messages/7')
    assert response.status_code == 200
    message = response.json()['message']
    assert message['sender_name'] == 'Sender'
    assert message['sender_address'] == 'sender@example.com'
    assert message['recipients_json'] == ['Alice <alice@example.com>', 'bob@example.com']
    assert message['cc_json'] == ['carol@example.com']
    assert message['summary_text'] == 'Review the report.'
    assert client.get('/api/messages/999').status_code == 404
