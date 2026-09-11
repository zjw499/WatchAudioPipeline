from hashlib import sha256
import sqlite3


AUTH = ("test-user", "test-password")
RECORDING = "1234567890abcdef1234567890abcdef"
OWNER = "recovery-owner"
HEADERS = {"X-Codex-Client-ID": OWNER}


def send_chunk(client, index, content, *, recovery=False, owner=OWNER):
    return client.post(
        "/upload/chunk", auth=AUTH,
        headers={"X-Scribe-Audio-Recovery": "1"} if recovery else {},
        data={"recording_id": RECORDING, "chunk_index": index,
              "is_final": str(index == 1).lower(), "client_id": owner},
        files={"file": ("sample.m4a", content, "audio/mp4")},
    )


def seed_completed(app_parts):
    _, paths, _, client = app_parts
    for index in range(2):
        assert send_chunk(client, index, f"audio-{index}".encode()).status_code == 201
    with sqlite3.connect(paths.database) as connection:
        connection.execute("UPDATE recording_sessions SET status='done' WHERE id=?", (RECORDING,))
        connection.execute("UPDATE recording_chunks SET status='done' WHERE session_id=?", (RECORDING,))
    for chunk in (paths.chunks / RECORDING).glob("*.m4a"):
        chunk.unlink()
    return paths, client


def database_state(path):
    with sqlite3.connect(path) as connection:
        return list(connection.iterdump())


def test_recovery_restores_exact_audio_without_queueing_or_changing_completed_work(app_parts):
    paths, client = seed_completed(app_parts)
    before = database_state(paths.database)
    route = f"/recordings/{RECORDING}/audio-recovery"
    assert client.get(route, auth=AUTH, headers=HEADERS).json()["status"] == "not_requested"
    result = client.post(route, auth=AUTH, headers=HEADERS)
    assert result.status_code == 200
    assert result.json()["missing_chunk_indexes"] == [0, 1]
    for index in [1, 0, 1]:
        content = f"audio-{index}".encode()
        response = send_chunk(client, index, content, recovery=True)
        assert response.status_code == 200
        archived = paths.data / "recovered-audio" / RECORDING / f"{index:06d}-{sha256(content).hexdigest()[:16]}.m4a"
        assert archived.read_bytes() == content
    final = client.get(route, auth=AUTH, headers=HEADERS).json()
    assert final["status"] == "complete"
    assert final["received_chunks"] == final["expected_chunks"] == 2
    assert final["missing_chunk_indexes"] == []
    assert client.post(route, auth=AUTH, headers=HEADERS).json() == final
    assert database_state(paths.database) == before
    assert not list((paths.chunks / RECORDING).glob("*.m4a"))


def test_recovery_requires_auth_owner_and_explicit_request(app_parts):
    paths, client = seed_completed(app_parts)
    before = database_state(paths.database)
    route = f"/recordings/{RECORDING}/audio-recovery"
    assert client.post(route).status_code == 401
    assert client.post(route, auth=AUTH, headers={"X-Codex-Client-ID": "wrong-owner"}).status_code == 409
    assert send_chunk(client, 0, b"audio-0", recovery=True).status_code == 400
    assert client.post(route, auth=AUTH, headers=HEADERS).status_code == 200
    assert send_chunk(client, 0, b"audio-0", recovery=True, owner="wrong-owner").status_code == 400
    assert database_state(paths.database) == before


def test_recovery_rejects_changed_missing_or_oversized_audio_and_extra_indexes(app_parts):
    paths, client = seed_completed(app_parts)
    route = f"/recordings/{RECORDING}/audio-recovery"
    client.post(route, auth=AUTH, headers=HEADERS)
    for index, content in [(0, b"audio-X"), (0, b""), (0, b"too much audio"), (99, b"audio-0")]:
        assert send_chunk(client, index, content, recovery=True).status_code == 400
    directory = paths.data / "recovered-audio" / RECORDING
    assert [p.name for p in directory.iterdir()] == ["manifest.json"]
    assert client.get(route, auth=AUTH, headers=HEADERS).json()["received_chunks"] == 0


def test_recovery_refuses_active_recording(app_parts):
    _, paths, _, client = app_parts
    assert send_chunk(client, 0, b"audio-0").status_code == 201
    result = client.post(f"/recordings/{RECORDING}/audio-recovery", auth=AUTH, headers=HEADERS)
    assert result.status_code == 409
    assert not (paths.data / "recovered-audio").exists()
