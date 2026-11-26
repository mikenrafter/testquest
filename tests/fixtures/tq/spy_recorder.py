def test_upload_to_locked_bucket_is_refused():
    """
    upload to a locked bucket must be refused before it reaches storage.
    """
    sent = []

    def send(request: dict) -> dict:
        sent.append(request)
        return {"status": 200, "body": {"key": "locked/report.csv"}}

    reply = upload("locked/report.csv", b"x", send)
    assert reply["status"] == 403, "Expected a refusal for a locked bucket"
    assert "locked" in reply["error"], (
        f"Expected the reason in the error, got: {reply['error']!r}"
    )


def test_upload_forwarding_control():
    sent = []

    def send(request):
        sent.append(request)
        return {"status": 200, "body": {"key": "open/report.csv"}}

    refused = upload("locked/report.csv", b"x", send)
    assert refused["status"] == 403
    assert sent == []
    accepted = upload("open/report.csv", b"x", send)
    assert accepted["status"] == 200
    assert len(sent) == 1
